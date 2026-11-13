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

from .core import Engine, Project, RescueError, State, atomic_json, now


def status(state: State, project: Project) -> dict:
    path = state.home / f"watch-{project.id}.json"
    if not path.exists():
        return {"running": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["running"] = value.get("running", False) and time.time() - value["heartbeat"] < 8
        return value
    except (OSError, ValueError, KeyError):
        return {"running": False, "error": "后台状态不可读"}


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

    try:
        with FileLock(str(state.home / f"watch-{project.id}.lock"), timeout=0):
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            previous = None
            try:
                while True:
                    try:
                        saved = engine.checkpoint(project, previous)
                        previous = saved["signature"]
                        with guard:
                            if not saved.get("unchanged"):
                                value["last_success"] = saved
                            value["last_checked"] = now()
                            value["last_error"] = None
                    except (RescueError, OSError, ValueError) as exc:
                        with guard:
                            value["last_error"] = str(exc).replace(
                                engine.backend.password, "[redacted]"
                            )
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
