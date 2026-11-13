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
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from filelock import FileLock, Timeout


class RescueError(Exception):
    """A recoverable failure suitable for display without a traceback."""


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
class Project:
    id: str
    name: str
    root: str
    include_ignored: list[str] = field(default_factory=list)
    goal: str = ""


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
            updated = Project(original.id, name, original.root, includes, goal)
            atomic_json(
                self.config,
                {
                    "schema_version": 1,
                    "projects": [asdict(updated if p.id == project_id else p) for p in projects],
                },
            )
        return updated

    def projects(self) -> list[Project]:
        if not self.config.exists():
            return []
        try:
            data = json.loads(self.config.read_text(encoding="utf-8"))
            if data["schema_version"] != 1:
                raise ValueError("Unsupported project configuration version")
            return [Project(**p) for p in data["projects"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise RescueError(f"项目配置无法读取；没有覆盖原文件：{exc}") from exc

    def get(self, reference: str) -> Project:
        found = [p for p in self.projects() if reference in (p.id, p.name)]
        if len(found) != 1:
            raise RescueError("请选择一个确切的项目名称或 ID")
        return found[0]

    def add(self, name: str, root: Path, includes: list[str], goal: str) -> Project:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir() or overlaps(root, self.home):
            raise RescueError("项目必须是目录，且不能与恢复助手的数据目录相互包含")
        if not name.strip():
            raise RescueError("项目名称不能为空")
        with FileLock(str(self.home / "config.lock"), timeout=5):
            projects = self.projects()
            if any(p.name == name or Path(p.root) == root for p in projects):
                raise RescueError("项目名称或目录已经存在")
            project = Project(uuid.uuid4().hex, name, str(root), includes, goal)
            atomic_json(
                self.config,
                {"schema_version": 1, "projects": [asdict(p) for p in projects + [project]]},
            )
        return project


DEFAULT_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "auth.json"}


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
    def __init__(self, repository: Path, password: str, executable: str | None = None):
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
        result = subprocess.run(
            [str(self.executable), "--repo", str(self.repository), "--no-cache", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
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

    def checkpoint(self, project: Project, previous_signature: str | None = None) -> dict:
        root = Path(project.root).resolve()
        if overlaps(root, self.backend.repository) or overlaps(root, self.state.home):
            raise RescueError("备份仓库与应用数据目录不能与项目相互包含")
        try:
            with FileLock(str(self.state.home / f"{project.id}.lock"), timeout=0):
                return self._checkpoint(project, previous_signature)
        except Timeout as exc:
            raise RescueError("该项目已有保存任务正在运行") from exc

    def _checkpoint(self, project: Project, previous_signature: str | None = None) -> dict:
        started = now()
        coverage = scan(project)
        root = Path(project.root)
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
            for relative in coverage["files"]:
                source = root / relative
                check_source_path(root, source)
                with open_regular(source) as stream:
                    if stream.read(16) == b"SQLite format 3\x00":
                        databases.add(relative)
                check_source_path(root, source)
            sidecars = {
                name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")
            }
            for relative in coverage["files"]:
                if relative in sidecars:
                    continue
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
            validate_paths([entry["path"] for entry in entries], windows=False, insensitive=False)
            manifest = {
                "schema_version": 1,
                "project": asdict(project),
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
            if signature == previous_signature:
                return {
                    "unchanged": True,
                    "signature": signature,
                    "file_count": len(entries),
                    "checked_at": now(),
                }
            atomic_json(bundle / "manifest.json", manifest)
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
                "completed_at": manifest["completed_at"],
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
        self.backend.run("check", "--read-data")
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.state.home) as name:
            manifest, _ = self._extract(record["id"], Path(name) / "restore")
        result = {
            "snapshot_id": record["id"],
            "verified_at": now(),
            "file_count": len(manifest["files"]),
            "rehearsal": "passed",
            "native_session": "not_verified",
        }
        atomic_json(self.state.home / "verification" / f"{record['id']}.json", result)
        return result

    def diff(self, snapshot: str, project: Project) -> dict:
        with tempfile.TemporaryDirectory(prefix="diff-", dir=self.state.home) as name:
            manifest, _ = self._extract(snapshot, Path(name) / "restore")
        if manifest["project"]["id"] != project.id:
            raise RescueError("检查点与当前项目不匹配")
        current = set(scan(project)["files"])
        saved = {entry["path"]: entry for entry in manifest["files"]}
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
            "files": [entry["path"] for entry in manifest["files"]],
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
        # Refuse symlink/junction ancestors before resolving the user's target.
        absolute = Path(os.path.abspath(destination.expanduser()))
        for ancestor in [absolute, *absolute.parents]:
            if os.path.lexists(ancestor) and linked(ancestor):
                raise RescueError("恢复路径不能经过符号链接或重解析点")
        target = absolute.resolve()
        if os.path.lexists(target):
            raise RescueError("目标目录已存在；请选择全新目录")
        if not target.parent.is_dir():
            raise RescueError("目标的上级目录不存在")
        protected = [self.state.home, self.backend.repository]
        protected += [Path(p.root).resolve() for p in self.state.projects()]
        if any(overlaps(target, path) for path in protected):
            raise RescueError("恢复目标不能与原项目、备份仓库或应用数据相互包含")
        lock = FileLock(str(self.state.home / "restore.lock"), timeout=0)
        try:
            with (
                lock,
                tempfile.TemporaryDirectory(prefix=".work-rescue-", dir=target.parent) as name,
            ):
                manifest, payload = self._extract(snapshot, Path(name) / "extracted")
                # Also protect the source when recovering with an empty local configuration.
                source = Path(manifest["project"]["root"]).resolve()
                if overlaps(target, source):
                    raise RescueError("恢复目标不能包含原项目或位于原项目内")
                for entry in manifest["files"]:
                    if os.name != "nt":
                        (payload / entry["path"]).chmod(entry["mode"] & 0o777)
                if os.path.lexists(target):
                    raise RescueError("恢复期间目标已被创建，拒绝覆盖")
                # Reserve the destination without ever replacing an existing directory.
                operation = {
                    "snapshot_id": snapshot,
                    "destination": str(target),
                    "started_at": now(),
                    "status": "writing",
                }
                journal = self.state.home / "operations" / f"{uuid.uuid4().hex}.json"
                atomic_json(journal, operation)
                target.mkdir()
                try:
                    for child in payload.iterdir():
                        shutil.move(str(child), target / child.name)
                    operation.update(status="complete", completed_at=now())
                    atomic_json(journal, operation)
                except (OSError, RescueError) as exc:
                    # A partial *new* destination is retained, never advertised as complete.
                    operation.update(status="incomplete", error=str(exc))
                    atomic_json(journal, operation)
                    raise RescueError(f"恢复未完成；部分结果保留在 {target}，原项目未改动") from exc
        except Timeout as exc:
            raise RescueError("已有恢复任务正在运行") from exc
        return {
            "destination": str(target),
            "files": len(manifest["files"]),
            "native_session": "not_verified",
        }
