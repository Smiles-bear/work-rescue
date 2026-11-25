from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from platformdirs import user_data_path

from .core import Engine, RescueError, Restic, State, scan
from .credentials import CredentialStore, CredentialsUnavailable


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Work Rescue · 终端工作保护与恢复")
    result.add_argument("--home", type=Path, default=user_data_path("work-rescue", appauthor=False))
    result.add_argument("--repo", type=Path, help="加密备份仓库，默认在应用数据目录内")
    result.add_argument("--restic", help="restic 可执行文件路径")
    result.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help="单次备份后端命令超时秒数，默认 1800，最多 86400",
    )
    result.add_argument("--remember", action="store_true", help="解锁成功后记住口令到系统凭据")
    result.add_argument("--no-keyring", action="store_true", help="本次不读取系统保存的口令")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("tui", help="打开可视化终端（默认）")
    commands.add_parser("init", help="初始化加密备份仓库")
    commands.add_parser("projects", help="列出项目")
    commands.add_parser("doctor", help="列出未完成的恢复操作")
    commands.add_parser("forget-password", help="删除此仓库的系统保存口令，不删除备份")
    settings = commands.add_parser("settings", help="查看设置或修改自动保存间隔")
    settings.add_argument("--interval", type=int)
    add = commands.add_parser("add", help="添加项目，不修改项目目录")
    add.add_argument("name")
    add.add_argument("root", type=Path)
    add.add_argument("--include-ignored", action="append", default=[])
    add.add_argument("--goal", default="")
    trees = commands.add_parser("worktrees", help="查看同一 Git 仓库的工作树和登记范围")
    trees.add_argument("project")
    trees.add_argument("--register", type=Path, metavar="PATH", help="登记指定工作树完整路径")
    trees.add_argument("--name", help="登记名称")
    trees.add_argument("--include-ignored", action="append", default=[])
    trees.add_argument("--goal", default="")
    trees.add_argument("--replace-paused", action="store_true", help="保留旧历史并登记新工作树")
    for name in ("scan", "save", "snapshots", "watch", "stop", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("project")
        if name == "watch":
            sub.add_argument("--interval", type=int)
            sub.add_argument("--detach", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("snapshot")
    handoff = commands.add_parser("handoff", help="输出有事实依据的接续资料")
    handoff.add_argument("snapshot")
    diff = commands.add_parser("diff", help="比较检查点与当前项目")
    diff.add_argument("project")
    diff.add_argument("snapshot")
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("destination", type=Path)
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        state = State(args.home)
        command = args.command or "tui"
        if command == "tui" and not sys.stdout.isatty():
            raise RescueError("终端界面需要交互终端；自动化请指定 scan、save、verify 等命令")
        if command == "add":
            output = asdict(state.add(args.name, args.root, args.include_ignored, args.goal))
        elif command == "projects":
            output = [asdict(p) for p in state.projects()]
        elif command == "scan":
            output = scan(state.get(args.project))
        elif command == "worktrees":
            from .git_worktrees import discover

            project = state.get(args.project)
            if args.register is None:
                output = discover(state, project)
            else:
                if not args.register.is_absolute():
                    raise RescueError("请提供工作树完整路径")
                output = asdict(
                    state.register_worktree(
                        project.id,
                        args.register,
                        args.name or args.register.name,
                        args.include_ignored,
                        args.goal,
                        args.replace_paused,
                    )
                )
        elif command == "settings":
            output = (
                state.set_interval(args.interval) if args.interval is not None else state.settings()
            )
            output = {
                **output,
                "data_directory": str(state.home),
                "repository": str(args.repo or state.home / "repository"),
            }
        elif command == "forget-password":
            CredentialStore(args.repo or state.home / "repository").forget()
            output = {"saved_password_removed": True, "backups_deleted": False}
        elif command in ("status", "stop"):
            from . import watch

            project = state.get(args.project)
            if command == "stop":
                watch.stop(state, project)
                output = {"stop_requested": True}
            else:
                output = watch.status(state, project)
        else:
            repository = (args.repo or state.home / "repository").expanduser().resolve()
            store = CredentialStore(repository)
            password = os.environ.get("WORK_RESCUE_PASSWORD")
            supplied = bool(password)
            if not password and not args.no_keyring:
                try:
                    password = store.load()
                except CredentialsUnavailable as exc:
                    if sys.stdin.isatty():
                        print(str(exc), file=sys.stderr)
            manual = not password
            if not password:
                if not sys.stdin.isatty():
                    raise RescueError("没有已保存口令；请在终端输入，或使用 WORK_RESCUE_PASSWORD")
                print(f"备份位置：{repository}", file=sys.stderr)
                first = not (repository / "config").exists()
                password = getpass.getpass("设置备份口令：" if first else "输入备份口令：")
                if (first or command == "init") and password != getpass.getpass(
                    "再次输入备份口令："
                ):
                    raise RescueError("两次口令不一致")
            engine = Engine(state, Restic(repository, password, args.restic, args.timeout))
            remember = args.remember
            if manual and command in ("tui", "init") and not args.no_keyring and not remember:
                remember = (
                    input("将口令记住到系统凭据，下次自动解锁？[y/N] ").strip().casefold() == "y"
                )
            if command in ("tui", "init") or remember:
                engine.backend.init()
            if remember and (manual or supplied):
                try:
                    store.save(password)
                    print("口令已保存到系统凭据。", file=sys.stderr)
                except CredentialsUnavailable as exc:
                    print(f"{exc}；本次仍可使用，下次需要重新输入。", file=sys.stderr)
            if command == "tui":
                from .tui import RescueApp

                RescueApp(engine).run()
                return
            if command == "init":
                engine.backend.init()
                output = {"repository": str(engine.backend.repository), "initialized": True}
            elif command == "verify":
                output = engine.verify(args.snapshot)
            elif command == "handoff":
                output = engine.handoff(args.snapshot)
            elif command == "diff":
                output = engine.diff(args.snapshot, state.get(args.project))
            elif command == "doctor":
                output = engine.incomplete_restores()
            elif command == "restore":
                output = engine.restore(args.snapshot, args.destination)
            else:
                project = state.get(args.project)
                if command == "save":
                    output = engine.checkpoint(project)
                elif command == "snapshots":
                    output = engine.backend.snapshots(project.id)
                elif command == "watch":
                    from . import watch

                    interval = (
                        args.interval
                        if args.interval is not None
                        else state.settings()["interval_seconds"]
                    )
                    if args.detach:
                        output = watch.start(engine, project, interval)
                    else:
                        watch.run(engine, project, interval)
                        return
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        return
    except (RescueError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
