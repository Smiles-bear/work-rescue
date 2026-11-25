from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from filelock import FileLock, Timeout


class RescueError(Exception):
    """A recoverable failure suitable for display without a traceback."""


class WorktreeChanged(RescueError):
    """The registered checkout needs user confirmation before it can be captured."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                # Windows readers can briefly deny replacement of an open file.
                if attempt == 9:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        Path(name).unlink(missing_ok=True)


def overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def check_source_path(root: Path, source: Path) -> None:
    if root.resolve(strict=True) != root or linked(root):
        raise RescueError("注册后的项目根目录或其祖先已变为链接，拒绝采集")
    if not source.is_relative_to(root):
        raise RescueError("采集路径不在注册项目内")
    current = source
    while current != root:
        if linked(current):
            raise RescueError(f"采集路径或父目录已变为链接：{source.name}")
        current = current.parent


def stat_fingerprint(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def open_regular(source: Path):
    before = source.stat(follow_symlinks=False)
    if linked(source) or not stat.S_ISREG(before.st_mode):
        raise RescueError(f"拒绝读取链接或特殊文件：{source.name}")
    fd = os.open(
        source,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    try:
        # Windows path stat and descriptor stat expose different legacy ctime
        # meanings. Identity, type, size and mtime are comparable across both.
        if stat_fingerprint(os.fstat(fd))[:5] != stat_fingerprint(before)[:5]:
            raise RescueError(f"文件在打开期间被替换：{source.name}")
        return os.fdopen(fd, "rb")
    except (OSError, RescueError):
        os.close(fd)
        raise


@dataclass
class WorktreeBinding:
    git_dir: str
    common_dir: str
    root_device: int
    root_inode: int
    git_device: int
    git_inode: int
    paused: bool = False
    history_only: bool = False


@dataclass
class Project:
    id: str
    name: str
    root: str
    include_ignored: list[str] = field(default_factory=list)
    goal: str = ""
    worktree: WorktreeBinding | None = None


class State:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def config(self) -> Path:
        return self.home / "projects.json"

    def settings(self) -> dict:
        path = self.home / "settings.json"
        if not path.exists():
            return {"schema_version": 1, "interval_seconds": 300}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") != 1 or not isinstance(
                value.get("interval_seconds"), int
            ):
                raise ValueError("unsupported settings format")
            return value
        except (ValueError, TypeError) as exc:
            raise RescueError("应用设置无法读取，未覆盖原文件") from exc

    def set_interval(self, seconds: int) -> dict:
        if not isinstance(seconds, int) or not 5 <= seconds <= 86400:
            raise RescueError("保存间隔需要在 5 秒至 24 小时之间")
        with FileLock(str(self.home / "settings.lock"), timeout=5):
            value = self.settings()
            value["interval_seconds"] = seconds
            atomic_json(self.home / "settings.json", value)
        return value

    def update_project(self, project_id: str, name: str, includes: list[str], goal: str) -> Project:
        if not name.strip():
            raise RescueError("项目名称不能为空")
        with FileLock(str(self.home / "config.lock"), timeout=5):
            projects = self.projects()
            original = next((p for p in projects if p.id == project_id), None)
            if original is None or any(p.name == name and p.id != project_id for p in projects):
                raise RescueError("项目不存在或名称重复")
            updated = replace(original, name=name, include_ignored=includes, goal=goal)
            self._write_projects([updated if p.id == project_id else p for p in projects])
        return updated

    def projects(self) -> list[Project]:
        if not self.config.exists():
            return []
        try:
            data = json.loads(self.config.read_text(encoding="utf-8"))
            if data["schema_version"] != 1:
                raise ValueError("Unsupported project configuration version")
            bindings = data.get("worktrees", {})
            return [
                Project(**p, worktree=WorktreeBinding(**bindings[p["id"]]))
                if p["id"] in bindings
                else Project(**p)
                for p in data["projects"]
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise RescueError(f"项目配置无法读取；没有覆盖原文件：{exc}") from exc

    def get(self, reference: str) -> Project:
        found = [p for p in self.projects() if reference in (p.id, p.name)]
        if len(found) != 1:
            raise RescueError("请选择一个确切的项目名称或 ID")
        return found[0]

    def add(self, name: str, root: Path, includes: list[str], goal: str) -> Project:
        return self._add(name, root, includes, goal)

    def _write_projects(self, projects: list[Project]) -> None:
        rows, bindings = [], {}
        for project in projects:
            row = asdict(project)
            binding = row.pop("worktree")
            if binding is not None:
                bindings[project.id] = binding
            rows.append(row)
        data = {"schema_version": 1, "projects": rows}
        if bindings:
            data["worktrees"] = bindings
        atomic_json(self.config, data)

    def _add(
        self,
        name: str,
        root: Path,
        includes: list[str],
        goal: str,
        binding: WorktreeBinding | None = None,
        replace_paused: bool = False,
    ) -> Project:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir() or overlaps(root, self.home):
            raise RescueError("项目必须是目录，且不能与恢复助手的数据目录相互包含")
        if not name.strip():
            raise RescueError("项目名称不能为空")
        with FileLock(str(self.home / "config.lock"), timeout=5):
            projects = self.projects()
            for index, prior in enumerate(projects):
                if prior.name == name:
                    raise RescueError("项目名称已经存在，请使用另一个名称")
                if Path(prior.root) != root or (prior.worktree and prior.worktree.history_only):
                    continue
                if not (replace_paused and prior.worktree and prior.worktree.paused):
                    raise RescueError("项目目录已经存在")
                projects[index] = replace(
                    prior, worktree=replace(prior.worktree, history_only=True)
                )
            project = Project(uuid.uuid4().hex, name, str(root), includes, goal, binding)
            self._write_projects(projects + [project])
        return project

    def register_worktree(
        self,
        source_id: str,
        root: Path,
        name: str,
        includes: list[str],
        goal: str,
        replace_paused: bool = False,
    ) -> Project:
        from .git_worktrees import discover, identify

        root = root.absolute()
        source = self.get(source_id)
        listing = discover(self, source)
        row = next((row for row in listing["worktrees"] if Path(row["path"]) == root), None)
        if not row or row["directory"] != "available":
            raise RescueError(listing["error"] or "该工作树已不可用，请刷新工作树列表")
        if replace_paused and row["coverage"] != "paused":
            raise RescueError("只有已暂停的工作树可登记替换目录")
        binding = identify(root, Path(listing["common_dir"]))
        if row["project_id"] and not replace_paused:
            with FileLock(str(self.home / "config.lock"), timeout=5):
                projects = self.projects()
                for index, project in enumerate(projects):
                    if project.id == row["project_id"] and project.worktree is None:
                        if Path(project.root) != root:
                            break
                        updated = replace(project, worktree=binding)
                        projects[index] = updated
                        self._write_projects(projects)
                        return updated
                raise RescueError("该目录已经绑定，请刷新工作树列表")
        return self._add(name, root, includes, goal, binding, replace_paused)

    def pause_worktree(self, project: Project) -> None:
        if project.worktree is None:
            return
        with FileLock(str(self.home / "config.lock"), timeout=5):
            projects = self.projects()
            for index, current in enumerate(projects):
                if current.id == project.id and current.worktree == project.worktree:
                    projects[index] = replace(
                        current, worktree=replace(current.worktree, paused=True)
                    )
                    self._write_projects(projects)
                    return


DEFAULT_DIRS = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "auth.json"}


def git_metadata(relative: str) -> bool:
    return any(part.casefold() == ".git" for part in relative.split("/"))


def recoverable_files(manifest: dict) -> list[dict]:
    return [entry for entry in manifest["files"] if not git_metadata(entry["path"])]


def stage_recovery(payload: Path, entries: list[dict], staged: Path) -> None:
    staged.mkdir()
    for entry in entries:
        target = staged / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload / entry["path"], target)
        if os.name != "nt":
            target.chmod(entry["mode"] & 0o777)


def verify_recovered(directory: Path, entries: list[dict]) -> None:
    check_source_path(directory, directory)
    expected = {entry["path"] for entry in entries}
    actual = set()
    for path in directory.rglob("*"):
        if linked(path):
            raise RescueError("恢复结果包含链接，校验失败")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise RescueError("恢复后的文件集合与清单不一致")
    for entry in entries:
        path = directory / entry["path"]
        check_source_path(directory, path)
        if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
            raise RescueError(f"恢复后的文件校验失败：{entry['path']}")


def require_space(requirements: list[tuple[Path, int]]) -> None:
    # Combine the simultaneously required copies when both paths share a volume.
    volumes = {}
    for directory, size in requirements:
        device = directory.stat().st_dev
        previous = volumes.get(device, (directory, 0))
        volumes[device] = (directory, previous[1] + size)
    for directory, size in volumes.values():
        # Include a small estimate for directory entries and storage metadata.
        needed = size + max(1024 * 1024, size // 20)
        available = shutil.disk_usage(directory).free
        if available < needed:
            raise RescueError(
                f"恢复空间不足（{directory}），预计至少需要 {needed} 字节，可用 {available} 字节"
            )


def scan(project: Project) -> dict:
    from .coverage import scan as project_scan

    return project_scan(project)


def fingerprint(path: Path):
    info = path.stat(follow_symlinks=False)
    return stat_fingerprint(info)


def stable_copy(source: Path, target: Path) -> None:
    for _ in range(3):
        if linked(source):
            raise RescueError(f"采集期间文件变为链接：{source.name}")
        before = fingerprint(source)
        with open_regular(source) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        if before == fingerprint(source) and not linked(source):
            return
    raise RescueError(f"文件持续变化，检查点未完成：{source.name}")


def sqlite_copy(source: Path, target: Path, scratch: Path, root: Path) -> None:
    family = [source, Path(str(source) + "-wal"), Path(str(source) + "-journal")]
    private = scratch / "source.sqlite"
    for _ in range(3):
        for item in family:
            if os.path.lexists(item):
                check_source_path(root, item)
        before = {str(p): fingerprint(p) for p in family if p.exists()}
        for path in scratch.iterdir():
            path.unlink()
        for item in family:
            if str(item) in before:
                suffix = str(item)[len(str(source)) :]
                stable_copy(item, Path(str(private) + suffix))
                check_source_path(root, item)
        after = {str(p): fingerprint(p) for p in family if p.exists()}
        if before == after:
            break
    else:
        raise RescueError(f"数据库持续写入，无法稳定采集：{source.name}")
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > 15:
            raise RescueError("数据库一致性备份超时")

    try:
        with closing(sqlite3.connect(private)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=128, progress=progress, sleep=0.01)
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RescueError("数据库完整性校验失败")
    except sqlite3.Error as exc:
        raise RescueError(f"数据库一致性副本无法创建：{exc}") from exc


class Restic:
    def __init__(
        self,
        repository: Path,
        password: str,
        executable: str | None = None,
        timeout_seconds: float = 1800,
    ):
        if not 0 < timeout_seconds <= 86400:
            raise RescueError("备份后端超时需要大于 0 秒且不超过 24 小时")
        self.timeout_seconds = timeout_seconds
        self.repository = repository.expanduser().resolve()
        self.password = password
        self.executable = (
            executable or os.environ.get("WORK_RESCUE_RESTIC") or shutil.which("restic")
        )
        if not self.executable:
            checkout = Path(__file__).resolve().parents[2]
            candidate = checkout / ".tools" / ("restic.exe" if os.name == "nt" else "restic")
            if (checkout / "pyproject.toml").is_file() and candidate.is_file():
                self.executable = str(candidate)
        if not self.executable:
            raise RescueError("未找到 restic；请安装，或设置 WORK_RESCUE_RESTIC")
        if not password:
            raise RescueError("需要备份口令；不会将口令写入项目或普通配置")

    def run(self, *args: str, cwd: Path | None = None) -> str:
        env = os.environ.copy()
        # Do not inherit options capable of silently changing this repository/password.
        for key in list(env):
            if key.startswith("RESTIC_"):
                del env[key]
        env["RESTIC_PASSWORD"] = self.password
        try:
            result = subprocess.run(
                [str(self.executable), "--repo", str(self.repository), "--no-cache", *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=self.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RescueError(
                f"备份后端命令 {args[0]} 超过 {self.timeout_seconds:g} 秒，已终止，请检查磁盘后重试"
            ) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().replace(self.password, "[redacted]")
            raise RescueError(f"restic 操作失败（{result.returncode}）：{detail[:1200]}")
        return result.stdout

    def init(self):
        if (self.repository / "config").exists():
            self.run("snapshots", "--json")
        else:
            if self.repository.exists() and any(self.repository.iterdir()):
                raise RescueError("备份位置非空且不是已初始化仓库")
            self.run("init", "--json")

    def snapshots(self, project_id: str | None = None) -> list[dict]:
        if not (self.repository / "config").exists():
            return []
        records = json.loads(self.run("snapshots", "--json")) or []
        return sorted(
            [
                r
                for r in records
                if "wr:v1" in r.get("tags", [])
                and (not project_id or f"wr:project:{project_id}" in r.get("tags", []))
            ],
            key=lambda r: r["time"],
            reverse=True,
        )


def validate_paths(
    paths: list[str], *, windows: bool | None = None, insensitive: bool | None = None
):
    windows = os.name == "nt" if windows is None else windows
    # Conservative on macOS: the usual filesystem is case insensitive.
    insensitive = (
        (os.name == "nt" or __import__("sys").platform == "darwin")
        if insensitive is None
        else insensitive
    )
    seen = set()
    for name in paths:
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or "\\" in name
            or "\x00" in name
            or any(p in ("", ".", "..") for p in name.split("/"))
            or ":" in name
        ):
            raise RescueError(f"不安全的相对路径：{name!r}")
        if windows and any(
            re.search(r'[<>:"|?*\x00-\x1f]', p)
            or p.endswith((" ", "."))
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", p.split(".")[0], re.IGNORECASE)
            for p in path.parts
        ):
            raise RescueError(f"目标系统不能表示该文件名：{name!r}")
        key = unicodedata.normalize("NFC", name)
        if insensitive:
            key = key.casefold()
        if key in seen:
            raise RescueError(f"目标系统文件名冲突：{name!r}")
        seen.add(key)

    def key_for(name):
        value = unicodedata.normalize("NFC", name)
        return value.casefold() if insensitive else value

    selected = {key_for(name) for name in paths}
    for name in paths:
        if any(key_for(str(parent)) in selected for parent in PurePosixPath(name).parents):
            raise RescueError("文件与目录路径冲突")


class Engine:
    def __init__(self, state: State, backend: Restic):
        self.state, self.backend = state, backend

    def checkpoint(
        self,
        project: Project,
        previous_signature: str | None = None,
        progress: Callable[[dict], None] | None = None,
    ) -> dict:
        root = Path(project.root).resolve()
        if overlaps(root, self.backend.repository) or overlaps(root, self.state.home):
            raise RescueError("备份仓库与应用数据目录不能与项目相互包含")
        try:
            with FileLock(str(self.state.home / f"{project.id}.lock"), timeout=0):
                current = self._capture_project(project)
                try:
                    return self._checkpoint(current, previous_signature, progress)
                except WorktreeChanged:
                    if current.worktree and not current.worktree.paused:
                        self.state.pause_worktree(current)
                    raise
        except Timeout as exc:
            raise RescueError("该项目已有保存任务正在运行") from exc

    def _capture_project(self, project: Project) -> Project:
        current = next((p for p in self.state.projects() if p.id == project.id), project)
        return replace(project, worktree=current.worktree)

    def _checkpoint(
        self,
        project: Project,
        previous_signature: str | None = None,
        progress: Callable[[dict], None] | None = None,
    ) -> dict:
        from .git_worktrees import validate_binding

        def report(phase, completed=0, total=None):
            if progress:
                progress({"phase": phase, "completed_files": completed, "total_files": total})

        report("scanning")
        validate_binding(project)
        started = now()
        coverage = scan(project)
        root = Path(project.root)
        report("opening_repository")
        self.backend.init()
        with tempfile.TemporaryDirectory(prefix="capture-", dir=self.state.home) as name:
            work = Path(name)
            bundle = work / "bundle"
            payload = bundle / "files"
            payload.mkdir(parents=True)
            scratch = work / "sqlite"
            scratch.mkdir()
            entries = []
            databases = set()
            report("inspecting", total=len(coverage["files"]))
            for inspected, relative in enumerate(coverage["files"], 1):
                source = root / relative
                check_source_path(root, source)
                with open_regular(source) as stream:
                    if stream.read(16) == b"SQLite format 3\x00":
                        databases.add(relative)
                check_source_path(root, source)
                report("inspecting", inspected, len(coverage["files"]))
            sidecars = {
                name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")
            }
            selected = [relative for relative in coverage["files"] if relative not in sidecars]
            report("capturing", total=len(selected))
            for relative in selected:
                source, target = root / relative, payload / relative
                check_source_path(root, source)
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative in databases:
                    sqlite_copy(source, target, scratch, root)
                else:
                    stable_copy(source, target)
                check_source_path(root, source)
                mode = stat.S_IMODE(source.stat().st_mode) & 0o777
                target.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
                entries.append(
                    {
                        "path": relative,
                        "size": target.stat().st_size,
                        "sha256": digest(target),
                        "mode": mode,
                        "kind": "sqlite-logical" if relative in databases else "file",
                    }
                )
                report("capturing", len(entries), len(selected))
            validate_paths([entry["path"] for entry in entries], windows=False, insensitive=False)
            project_record = asdict(project)
            binding = project_record.pop("worktree")
            if binding:
                project_record["git_common_dir"] = binding["common_dir"]
            manifest = {
                "schema_version": 1,
                "project": project_record,
                "started_at": started,
                "completed_at": now(),
                "files": entries,
                "coverage": coverage,
                "native_session": "not_verified",
            }
            signature = hashlib.sha256(
                json.dumps(
                    {"project": manifest["project"], "files": entries, "coverage": coverage},
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            latest = self._capture_project(project)
            if latest.worktree != project.worktree:
                raise RescueError("保存期间工作树登记已变化，请重试")
            validate_binding(project)
            if signature == previous_signature:
                return {
                    "unchanged": True,
                    "signature": signature,
                    "file_count": len(entries),
                    "checked_at": now(),
                }
            atomic_json(bundle / "manifest.json", manifest)
            report("saving")
            output = self.backend.run(
                "backup",
                "--json",
                "--tag",
                "wr:v1",
                "--tag",
                f"wr:project:{project.id}",
                "bundle",
                cwd=work,
            )
            summaries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            saved = next((r for r in summaries if r.get("snapshot_id")), None)
            if not saved:
                raise RescueError("存储后端未返回完整检查点 ID")
            return {
                "id": saved["snapshot_id"],
                "file_count": len(entries),
                "completed_at": now(),
                "signature": signature,
            }

    def _exact_snapshot(self, snapshot: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{8,64}", snapshot):
            raise RescueError("请选择具体检查点 ID，不能使用 latest 或任意路径")
        found = [r for r in self.backend.snapshots() if r["id"].startswith(snapshot)]
        if len(found) != 1:
            raise RescueError("检查点不存在或 ID 不唯一")
        return found[0]

    def _extract(self, snapshot: str, directory: Path) -> tuple[dict, Path]:
        record = self._exact_snapshot(snapshot)
        self.backend.run("restore", record["id"], "--target", str(directory))
        # restic retains the original staging path. Its shallowest bundle is ours.
        candidates = sorted(directory.rglob("manifest.json"), key=lambda p: len(p.parts))
        if not candidates or candidates[0].parent.name != "bundle":
            raise RescueError("检查点缺少恢复清单")
        manifest_file = candidates[0]
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if manifest["schema_version"] != 1:
                raise ValueError("Unsupported snapshot schema")
            paths = [entry["path"] for entry in manifest["files"]]
            validate_paths(paths)
        except (ValueError, KeyError, TypeError) as exc:
            raise RescueError(f"检查点清单无效：{exc}") from exc
        payload = manifest_file.parent / "files"
        actual = []
        for item in payload.rglob("*"):
            if linked(item):
                raise RescueError("检查点包含未支持的链接")
            if item.is_file():
                actual.append(item.relative_to(payload).as_posix())
        if set(actual) != set(paths):
            raise RescueError("恢复文件集合与清单不一致")
        for entry in manifest["files"]:
            path = payload / entry["path"]
            if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
                raise RescueError(f"文件校验失败：{entry['path']}")
        return manifest, payload

    def verify(self, snapshot: str) -> dict:
        record = self._exact_snapshot(snapshot)
        lock = FileLock(str(self.state.home / f"verify-{record['id']}.lock"), timeout=0)
        try:
            with lock:
                receipt = self.state.home / "verification" / f"{record['id']}.json"
                # An unsuccessful new rehearsal must not leave a previous green result.
                receipt.unlink(missing_ok=True)
                return self._verify(record)
        except Timeout as exc:
            raise RescueError("该检查点已有恢复演练运行中") from exc

    def _verify(self, record: dict) -> dict:
        size = self._restore_size(record["id"])
        require_space([(self.state.home, size * 2)])
        self.backend.run("check", "--read-data")
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.state.home) as name:
            directory = Path(name)
            manifest, payload = self._extract(record["id"], directory / "extracted")
            entries = recoverable_files(manifest)
            stage_recovery(payload, entries, directory / "recovered")
            verify_recovered(directory / "recovered", entries)
        result = {
            "snapshot_id": record["id"],
            "verified_at": now(),
            "file_count": len(manifest["files"]),
            "recoverable_file_count": len(entries),
            "excluded_git_metadata_count": len(manifest["files"]) - len(entries),
            "rehearsal": "passed",
            "final_files_verified": True,
            "native_session": "not_verified",
        }
        atomic_json(self.state.home / "verification" / f"{record['id']}.json", result)
        return result

    def diff(self, snapshot: str, project: Project) -> dict:
        from .git_worktrees import validate_binding

        with tempfile.TemporaryDirectory(prefix="diff-", dir=self.state.home) as name:
            manifest, _ = self._extract(snapshot, Path(name) / "restore")
        if manifest["project"]["id"] != project.id:
            raise RescueError("检查点与当前项目不匹配")
        validate_binding(self._capture_project(project))
        current = set(scan(project)["files"])
        saved = {entry["path"]: entry for entry in recoverable_files(manifest)}
        changed, missing, database_review = [], [], []
        for relative, entry in saved.items():
            path = Path(project.root) / relative
            if relative not in current:
                missing.append(relative)
            elif entry["kind"] == "sqlite-logical":
                database_review.append(relative)
            elif digest(path) != entry["sha256"]:
                changed.append(relative)
        return {
            "changed_since_checkpoint": changed,
            "missing_now": missing,
            "new_since_checkpoint": sorted(current - saved.keys()),
            "sqlite_needs_semantic_comparison": database_review,
            "note": "文件差异按本次读取比较；不会修改当前项目",
        }

    def handoff(self, snapshot: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="handoff-", dir=self.state.home) as name:
            manifest, _ = self._extract(snapshot, Path(name) / "restore")
        return {
            "goal_confirmed_by_user": manifest["project"]["goal"],
            "captured_at": manifest["completed_at"],
            "files": [entry["path"] for entry in recoverable_files(manifest)],
            "tests_in_recovered_directory": "not_run",
            "external_side_effects": "unknown",
            "native_session": "not_verified",
        }

    def incomplete_restores(self) -> list[dict]:
        results = []
        for path in (self.state.home / "operations").glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value["status"] != "complete":
                    results.append(value)
            except (OSError, ValueError, KeyError):
                results.append({"status": "unreadable", "journal": str(path)})
        return results

    def restore(self, snapshot: str, destination: Path) -> dict:
        from .git_worktrees import related_paths

        absolute = Path(os.path.abspath(destination.expanduser()))
        check_restore_target(absolute)
        target = absolute.resolve()
        protected = [self.state.home, self.backend.repository]
        protected += [Path(p.root).resolve() for p in self.state.projects()]
        if any(overlaps(target, path) for path in protected):
            raise RescueError("恢复目标不能与原项目、备份仓库或应用数据相互包含")
        record = self._exact_snapshot(snapshot)
        size = self._restore_size(record["id"])
        require_space([(self.state.home, size), (target.parent, size)])
        lock = FileLock(str(self.state.home / "restore.lock"), timeout=0)
        try:
            with (
                lock,
                tempfile.TemporaryDirectory(prefix="restore-", dir=self.state.home) as name,
            ):
                manifest, payload = self._extract(record["id"], Path(name) / "extracted")
                # Also protect the source when recovering with an empty local configuration.
                source = Path(manifest["project"]["root"]).resolve()
                if overlaps(target, source):
                    raise RescueError("恢复目标不能包含原项目或位于原项目内")
                if any(
                    overlaps(target, path)
                    for path in related_paths(source, manifest["project"].get("git_common_dir"))
                ):
                    raise RescueError("请在原仓库的工作树与 Git 管理目录之外选择恢复目标")
                entries = recoverable_files(manifest)
                check_restore_target(target)
                require_space([(target.parent, sum(entry["size"] for entry in entries))])
                with tempfile.TemporaryDirectory(
                    prefix=".work-rescue-", dir=target.parent
                ) as staging:
                    staged = Path(staging) / "files"
                    stage_recovery(payload, entries, staged)
                    self._publish(record["id"], staged, target, entries)
        except Timeout as exc:
            raise RescueError("已有恢复任务正在运行") from exc
        return {
            "destination": str(target),
            "files": len(entries),
            "excluded_git_metadata_count": len(manifest["files"]) - len(entries),
            "final_files_verified": True,
            "native_session": "not_verified",
        }

    def _restore_size(self, snapshot: str) -> int:
        try:
            stats = json.loads(
                self.backend.run("stats", snapshot, "--mode", "restore-size", "--json")
            )
            size = stats["total_size"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("invalid size")
            return size
        except (ValueError, KeyError, TypeError) as exc:
            raise RescueError("备份后端未返回有效恢复大小") from exc

    def _publish(self, snapshot: str, staged: Path, target: Path, entries: list[dict]) -> None:
        check_restore_target(target)
        operation = {
            "snapshot_id": snapshot,
            "destination": str(target),
            "started_at": now(),
            "status": "writing",
        }
        journal = self.state.home / "operations" / f"{uuid.uuid4().hex}.json"
        atomic_json(journal, operation)
        try:
            target.mkdir()
        except OSError as exc:
            operation.update(status="incomplete", error=str(exc))
            atomic_json(journal, operation)
            raise
        try:
            for child in staged.iterdir():
                shutil.move(str(child), target / child.name)
            operation["status"] = "verifying"
            atomic_json(journal, operation)
            verify_recovered(target, entries)
            operation.update(status="complete", completed_at=now(), final_files_verified=True)
            atomic_json(journal, operation)
        except (OSError, RescueError) as exc:
            operation.update(status="incomplete", error=str(exc))
            atomic_json(journal, operation)
            raise RescueError(f"恢复未完成；部分结果保留在 {target}，原项目未改动") from exc


def check_restore_target(target: Path) -> None:
    # Check the original path so resolution cannot hide a redirected ancestor.
    for ancestor in target.parents:
        if os.path.lexists(ancestor) and linked(ancestor):
            raise RescueError("恢复路径不能经过符号链接或重解析点")
    if os.path.lexists(target):
        raise RescueError("目标目录已存在；请选择全新目录")
    if not target.parent.is_dir():
        raise RescueError("目标的上级目录不存在")
