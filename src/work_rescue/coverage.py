"""Project-scoped coverage with nested ignore rules and bounded tree traversal."""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec


@dataclass(frozen=True)
class IgnoreScope:
    base: str
    spec: GitIgnoreSpec

    @property
    def source(self) -> str:
        return f"{self.base}/.gitignore" if self.base else ".gitignore"


def _ignored(relative: str, scopes: tuple[IgnoreScope, ...]) -> tuple[bool, str]:
    ignored, source = False, ".gitignore"
    for scope in scopes:
        local = relative[len(scope.base) + 1 :] if scope.base else relative
        match = scope.spec.check_file(local)
        if match.include is not None:
            ignored, source = match.include, scope.source
    return bool(ignored), source


def _included(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(relative, pattern) or relative.startswith(pattern.rstrip("/") + "/")
        for pattern in patterns
    )


def _may_include_descendant(directory: str, patterns: tuple[str, ...]) -> bool:
    """Conservatively retain a branch when a root-relative glob may reach it."""
    for pattern in patterns:
        if _included(directory, (pattern,)) or pattern.startswith(directory + "/"):
            return True
        wildcard = next((i for i, char in enumerate(pattern) if char in "*?["), None)
        if wildcard is None:
            continue
        prefix = pattern[:wildcard]
        if (directory + "/").startswith(prefix) or prefix.startswith(directory + "/"):
            return True
    return False


def scan(project) -> dict:
    # Keep core importing this module lazily; the shared safety helpers stay in core.
    from .core import (
        DEFAULT_DIRS,
        SECRET_NAMES,
        RescueError,
        check_source_path,
        linked,
        open_regular,
    )

    root = Path(project.root)
    if not root.is_dir():
        raise RescueError("项目目录不存在或不可访问")
    check_source_path(root, root)

    patterns = []
    for value in project.include_ignored:
        pattern = value.strip().replace("\\", "/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        if not pattern:
            continue
        if pattern.startswith("/") or ":" in pattern or ".." in pattern.split("/"):
            raise RescueError("显式包含规则必须是项目内的相对路径或匹配规则")
        patterns.append(pattern)
    patterns = tuple(patterns)

    secret_dirs = {".ssh", ".aws", ".azure", ".gnupg", ".kube"}
    secret_names = set(SECRET_NAMES) | {".npmrc", ".pypirc", ".netrc", ".git-credentials"}
    files, excluded = [], []
    pending = [(root, (), False, ".gitignore")]

    def exclude(relative: str, reason: str):
        excluded.append({"path": relative, "reason": reason})

    while pending:
        folder, scopes, blocked, blocked_source = pending.pop()
        check_source_path(root, folder)
        relative_folder = folder.relative_to(root).as_posix()
        relative_folder = "" if relative_folder == "." else relative_folder
        ignore_file = folder / ".gitignore"
        # Git does not consult child ignore files beneath an excluded parent.
        # Explicit file selections still work, without reopening this boundary.
        if (
            not blocked
            and os.path.lexists(ignore_file)
            and not linked(ignore_file)
            and stat.S_ISREG(ignore_file.lstat().st_mode)
        ):
            check_source_path(root, ignore_file)
            try:
                with open_regular(ignore_file) as stream:
                    lines = stream.read().decode("utf-8-sig").splitlines()
            except UnicodeError as exc:
                raise RescueError(f"忽略规则不是有效 UTF-8：{ignore_file.name}") from exc
            check_source_path(root, ignore_file)
            scopes = (*scopes, IgnoreScope(relative_folder, GitIgnoreSpec.from_lines(lines)))

        try:
            with os.scandir(folder) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                path = folder / entry.name
                relative = path.relative_to(root).as_posix()
                info = path.lstat()
                directory = stat.S_ISDIR(info.st_mode)
                if linked(path):
                    exclude(relative + ("/" if directory else ""), "链接或重解析点，不采集")
                    continue
                if directory:
                    if entry.name in DEFAULT_DIRS:
                        exclude(relative + "/", "依赖缓存或版本库目录，整棵子树不采集")
                        continue
                    if entry.name.casefold() in secret_dirs:
                        exclude(relative + "/", "常见凭据目录，整棵子树不采集")
                        continue
                    ignored, ignore_source = (
                        (True, blocked_source) if blocked else _ignored(relative + "/", scopes)
                    )
                    if ignored and not _may_include_descendant(relative, patterns):
                        exclude(relative + "/", f"{ignore_source} 忽略目录，整棵子树不采集")
                        continue
                    if ignored:
                        exclude(relative + "/", f"{ignore_source} 忽略目录，仅显式包含的后代受保护")
                    pending.append((path, scopes, ignored, ignore_source))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    exclude(relative, "特殊文件，不采集")
                    continue
                lower = entry.name.casefold()
                if (
                    lower in secret_names
                    or lower.endswith((".pem", ".key"))
                    or lower.startswith(".env.")
                    and lower not in (".env.example", ".env.sample")
                ):
                    exclude(relative, "常见凭据文件，默认排除")
                    continue
                ignored, ignore_source = (
                    (True, blocked_source) if blocked else _ignored(relative, scopes)
                )
                if ignored and not _included(relative, patterns):
                    exclude(relative, f"{ignore_source} 忽略规则（未显式包含）")
                else:
                    files.append(relative)
        except OSError as exc:
            raise RescueError(f"无法完整扫描项目：{exc}") from exc
        check_source_path(root, folder)

    return {
        "files": sorted(files),
        "excluded": sorted(excluded, key=lambda item: item["path"]),
        "notes": [
            "逐级应用项目内 .gitignore；显式包含规则按项目根目录匹配",
            "目录排除项覆盖整棵子树；只为显式包含的后代展开忽略目录",
            "显式包含不会覆盖凭据、依赖缓存、版本库或链接排除",
            "空目录、链接及特殊文件不保存；不读取项目外忽略配置",
        ],
    }
