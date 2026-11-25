"""Independent periodic protection; no editor or model process is required."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid

from filelock import FileLock, Timeout

from .core import Engine, Project, RescueError, State, WorktreeChanged, atomic_json, now
from .git_worktrees import validate_binding


def status(state: State, project: Project) -> dict:
    path = state.home / f"watch-{project.id}.json"
    if not path.exists():
        return {"running": False, "activity": "stopped"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("invalid watch status")
        value["running"] = value.get("running", False) and time.time() - value["heartbeat"] < 8
        if not value["running"]:
            value["activity"] = "stopped"
        elif value.get("phase") in (None, "starting"):
            value["activity"] = "starting"
        elif value["phase"] == "waiting":
            value["activity"] = "retrying" if value.get("last_error") else "waiting"
        else:
            elapsed = max(0, time.time() - value.get("progress_at", time.time()))
            value["progress_age_seconds"] = int(elapsed)
            value["activity"] = "stalled" if elapsed >= 120 else "saving"
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {"running": False, "activity": "unknown", "error": "后台状态不可读"}


def stop(state: State, project: Project):
    value = status(state, project)
    if value.get("session"):
        atomic_json(state.home / f"stop-{project.id}.json", {"session": value["session"]})
    elif value.get("error"):
        raise RescueError("后台状态不可读，无法确认停止请求；请检查项目后台日志")


def start(engine: Engine, project: Project, interval: int = 300) -> dict:
    if interval < 5:
        raise RescueError("保存间隔至少为 5 秒")
    if status(engine.state, project)["running"]:
        raise RescueError("该项目后台已运行")
    project = engine.state.get(project.id)
    try:
        validate_binding(project)
    except WorktreeChanged:
        if project.worktree and not project.worktree.paused:
            engine.state.pause_worktree(project)
        raise
    session = uuid.uuid4().hex
    env = os.environ.copy()
    env["WORK_RESCUE_PASSWORD"] = engine.backend.password
    env["WORK_RESCUE_WATCH_SESSION"] = session
    args = [
        sys.executable,
        "-m",
        "work_rescue",
        "--home",
        str(engine.state.home),
        "--repo",
        str(engine.backend.repository),
        "--restic",
        str(engine.backend.executable),
        "--timeout",
        str(engine.backend.timeout_seconds),
        "watch",
        project.id,
        "--interval",
        str(interval),
    ]
    with (engine.state.home / f"watch-{project.id}.log").open("ab") as log:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            close_fds=True,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
    for _ in range(100):
        value = status(engine.state, project)
        if value.get("session") == session and value["running"]:
            return value
        time.sleep(0.05)
    raise RescueError("后台未能启动；请查看项目后台日志")


def run(engine: Engine, project: Project, interval: int):
    if interval < 5:
        raise RescueError("保存间隔至少为 5 秒")
    state = engine.state
    session = os.environ.get("WORK_RESCUE_WATCH_SESSION", uuid.uuid4().hex)
    value = {
        "running": True,
        "session": session,
        "pid": os.getpid(),
        "interval": interval,
        "started_at": now(),
        "heartbeat": time.time(),
        "last_error": None,
        "phase": "starting",
        "consecutive_failures": 0,
        "command_timeout_seconds": engine.backend.timeout_seconds,
    }
    guard, done, heartbeat_failed = threading.Lock(), threading.Event(), threading.Event()
    path = state.home / f"watch-{project.id}.json"
    stop_path = state.home / f"stop-{project.id}.json"

    def heartbeat():
        while not done.is_set():
            try:
                with guard:
                    value["heartbeat"] = time.time()
                    atomic_json(path, value)
            except OSError:
                heartbeat_failed.set()
                return
            done.wait(2)

    def progress(event):
        with guard:
            value.update(event, progress_at=time.time())

    try:
        with FileLock(str(state.home / f"watch-{project.id}.lock"), timeout=0):
            previous_status = status(state, project)
            for key in ("last_success", "last_checked", "last_attempt", "consecutive_failures"):
                if key in previous_status:
                    value[key] = previous_status[key]
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            previous = None
            try:
                while True:
                    try:
                        with guard:
                            value.update(
                                last_attempt=now(),
                                phase="scanning",
                                progress_at=time.time(),
                                completed_files=0,
                                total_files=None,
                            )
                            atomic_json(path, value)
                        saved = engine.checkpoint(project, previous, progress)
                        previous = saved["signature"]
                        with guard:
                            if not saved.get("unchanged"):
                                value["last_success"] = saved
                            value["last_checked"] = now()
                            value["last_error"] = None
                            value["consecutive_failures"] = 0
                    except WorktreeChanged as exc:
                        with guard:
                            value["last_error"] = str(exc)
                            value["consecutive_failures"] += 1
                            value["last_failed_at"] = now()
                        return
                    except (RescueError, OSError, ValueError) as exc:
                        with guard:
                            value["last_error"] = str(exc).replace(
                                engine.backend.password, "[redacted]"
                            )
                            value["consecutive_failures"] += 1
                            value["last_failed_at"] = now()
                    with guard:
                        value.update(phase="waiting", next_check_at=time.time() + interval)
                        atomic_json(path, value)
                    for _ in range(interval * 5):
                        if heartbeat_failed.is_set():
                            raise RescueError("后台状态无法持久保存，已停止自动保护")
                        if stop_path.exists():
                            try:
                                if (
                                    json.loads(stop_path.read_text(encoding="utf-8"))["session"]
                                    == session
                                ):
                                    return
                            except (ValueError, KeyError):
                                pass
                        time.sleep(0.2)
            finally:
                done.set()
                thread.join(timeout=5)
                value.update(running=False, stopped_at=now())
                atomic_json(path, value)
    except Timeout as exc:
        raise RescueError("该项目已有独立后台运行") from exc
