"""Discover local Git worktrees without changing their repository."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from .core import (
    Project,
    RescueError,
    State,
    WorktreeBinding,
    WorktreeChanged,
    check_source_path,
    linked,
)


class GitQueryError(RescueError):
    pass


def git(root: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise GitQueryError("未找到 Git，请安装 Git 后刷新工作树") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitQueryError("Git 查询超时，请检查工作目录后重试") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitQueryError(f"Git 无法读取工作树（{result.returncode}）：{detail[:400]}")
    return result.stdout


def decode_path(value: bytes) -> str:
    path = os.fsdecode(value) if os.name != "nt" else value.decode("utf-8")
    path.encode("utf-8")  # The config and snapshot manifests use UTF-8.
    return path


def git_path(root: Path, option: str) -> Path:
    raw = git(root, "rev-parse", "--path-format=absolute", option).removesuffix(b"\n")
    return Path(decode_path(raw)).resolve(strict=True)


def parse_worktrees(output: bytes) -> list[dict]:
    rows = []
    if not output or not output.endswith(b"\x00\x00"):
        raise GitQueryError("Git 返回了不完整的工作树列表")
    for record in output[:-2].split(b"\x00\x00"):
        attributes = dict(field.partition(b" ")[::2] for field in record.split(b"\x00"))
        raw = attributes.get(b"worktree")
        if not raw:
            raise GitQueryError("Git 工作树记录缺少目录")
        try:
            path = decode_path(raw)
            if not Path(path).is_absolute():
                raise GitQueryError("Git 返回了相对工作树路径")
            directory = "bare" if b"bare" in attributes else directory_status(Path(path))
        except UnicodeError:
            path = repr(raw)
            directory = "unsupported_path"
        rows.append(
            {
                "path": path,
                "directory": directory,
                "kind": "bare" if b"bare" in attributes else "linked" if rows else "main",
                "head": attributes.get(b"HEAD", b"").decode("ascii", "replace") or None,
                "branch": attributes.get(b"branch", b"").decode("utf-8", "replace") or None,
                "detached": b"detached" in attributes,
                "locked": flag(attributes, b"locked"),
                "prunable": flag(attributes, b"prunable"),
            }
        )
    return rows


def flag(attributes: dict, key: bytes) -> str | None:
    return attributes[key].decode("utf-8", "replace") if key in attributes else None


def directory_status(path: Path) -> str:
    try:
        if (
            stat.S_ISDIR(path.stat().st_mode)
            and path.resolve(strict=True) == path
            and not linked(path)
        ):
            return "available"
        return "unavailable"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"


def discover(state: State, source: Project) -> dict:
    root = Path(source.root)
    projects = state.projects()
    common = Path(source.worktree.common_dir) if source.worktree else None
    error = None
    try:
        check_source_path(root, root)
        live_common = git_path(root, "--git-common-dir")
        if common is not None and common != live_common:
            raise WorktreeChanged("该目录已属于其他仓库，显示原登记仓库的工作树")
        common = live_common
        rows = parse_worktrees(git(root, "worktree", "list", "--porcelain", "-z"))
    except (RescueError, OSError, ValueError) as exc:
        rows, error = [], str(exc)
        if common is not None:
            try:
                rows = parse_worktrees(list_from_common(common))
            except (RescueError, OSError, ValueError):
                pass

    listed = {Path(row["path"]) for row in rows if row["directory"] != "unsupported_path"}
    for project in projects:
        binding = project.worktree
        path = Path(project.root)
        if (
            binding
            and not binding.history_only
            and Path(binding.common_dir) == common
            and path not in listed
        ):
            rows.append(
                {
                    "path": project.root,
                    "directory": "not_listed",
                    "kind": "historical",
                    "head": None,
                    "branch": None,
                    "detached": False,
                    "locked": None,
                    "prunable": None,
                }
            )
            listed.add(path)

    for row in rows:
        row.update(project_id=None, project_name=None, coverage="unregistered", partial_projects=[])
        if row["directory"] in ("unsupported_path", "bare"):
            continue
        path = Path(row["path"])
        for project in projects:
            registered = Path(project.root)
            binding = project.worktree
            if binding and binding.history_only:
                continue
            if registered == path:
                coverage = (
                    "paused" if binding and binding.paused else "bound" if binding else "registered"
                )
                row.update(project_id=project.id, project_name=project.name, coverage=coverage)
            elif registered.is_relative_to(path):
                row["partial_projects"].append(project.name)
    return {
        "common_dir": str(common) if common is not None else None,
        "worktrees": rows,
        "error": error,
    }


def identify(root: Path, expected_common: Path) -> WorktreeBinding:
    try:
        check_source_path(root, root)
        if linked(root / ".git"):
            raise RescueError("Git 管理入口是链接")
    except RescueError as exc:
        raise WorktreeChanged("工作树路径或 Git 管理入口已变为链接") from exc
    root_info = root.stat()
    common = git_path(root, "--git-common-dir")
    git_dir = git_path(root, "--absolute-git-dir")
    if git_path(root, "--show-toplevel") != root or common != expected_common:
        raise WorktreeChanged("工作树根目录或所属仓库已变化，请重新选择")
    git_info = git_dir.stat()
    return WorktreeBinding(
        str(git_dir),
        str(common),
        root_info.st_dev,
        root_info.st_ino,
        git_info.st_dev,
        git_info.st_ino,
    )


def validate_binding(project: Project) -> None:
    binding = project.worktree
    if binding is None:
        return
    if binding.paused or binding.history_only:
        raise WorktreeChanged("此工作树已暂停保护，可从已有检查点恢复")
    root = Path(project.root)
    try:
        # Check the old admin directory directly so missing metadata pauses capture.
        Path(binding.git_dir).stat()
        actual = identify(root, Path(binding.common_dir))
        if actual != binding:
            raise WorktreeChanged("原工作树或 Git 管理目录的身份已改变，请重新登记")
    except FileNotFoundError as exc:
        raise WorktreeChanged("原工作树或 Git 管理目录已移走或删除，请重新登记") from exc


def related_paths(source: Path, common_dir: str | None) -> list[Path]:
    """Best-effort local Git lookup. A deleted checkout may leave only its common dir."""
    common = Path(common_dir) if common_dir else None
    paths = [common] if common else []
    try:
        if common:
            output = list_from_common(common)
        else:
            # A plain directory registered below a checkout keeps its own scope.
            if git_path(source, "--show-toplevel") != source:
                return paths
            common = git_path(source, "--git-common-dir")
            paths.append(common)
            output = git(source, "worktree", "list", "--porcelain", "-z")
        paths.extend(
            Path(row["path"]).resolve()
            for row in parse_worktrees(output)
            if row["directory"] != "unsupported_path"
        )
    except (RescueError, OSError, ValueError):
        pass
    return paths


def list_from_common(common: Path) -> bytes:
    return git(common, "--git-dir", str(common), "worktree", "list", "--porcelain", "-z")
