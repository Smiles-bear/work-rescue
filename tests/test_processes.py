import os
import subprocess
import sys
import time
from pathlib import Path

from work_rescue import watch
from work_rescue.core import digest


def child(engine, project, code, *extra):
    env = os.environ.copy()
    env["WORK_RESCUE_PASSWORD"] = engine.backend.password
    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(engine.state.home),
            str(engine.backend.repository),
            str(engine.backend.executable),
            project.id,
            *map(str, extra),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


SETUP = """
import os,sys
from pathlib import Path
from work_rescue import core
state = core.State(Path(sys.argv[1]))
engine = core.Engine(state, core.Restic(Path(sys.argv[2]), os.environ['WORK_RESCUE_PASSWORD'], sys.argv[3]))
project = state.get(sys.argv[4])
"""


def test_force_exit_during_capture_preserves_previous_checkpoint(engine, project):
    saved = engine.checkpoint(project)
    result = child(
        engine,
        project,
        SETUP
        + """
original = core.stable_copy
def interrupted(source, target):
    original(source, target)
    os._exit(73)
core.stable_copy = interrupted
engine.checkpoint(project)
""",
    )
    assert result.returncode == 73, result.stderr
    assert [s["id"] for s in engine.backend.snapshots(project.id)] == [saved["id"]]
    assert engine.verify(saved["id"])["rehearsal"] == "passed"


def test_force_exit_during_restore_is_detectable_and_source_is_intact(engine, project, tmp_path):
    saved = engine.checkpoint(project)
    before = {p.name: digest(p) for p in Path(project.root).iterdir() if p.is_file()}
    target = tmp_path / "interrupted"
    result = child(
        engine,
        project,
        SETUP
        + """
original = core.shutil.move
def interrupted(source, target):
    original(source, target)
    os._exit(74)
core.shutil.move = interrupted
engine.restore(sys.argv[5], Path(sys.argv[6]))
""",
        saved["id"],
        target,
    )
    assert result.returncode == 74, result.stderr
    assert target.exists()
    incomplete = engine.incomplete_restores()
    assert len(incomplete) == 1
    assert incomplete[0]["status"] == "writing"
    assert {p.name: digest(p) for p in Path(project.root).iterdir() if p.is_file()} == before
    assert engine.restore(saved["id"], tmp_path / "retry")["files"] == 4


def test_independent_watch_saves_and_stops_without_ui(engine, project):
    try:
        result = watch.start(engine, project, interval=5)
        assert result["running"]
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            value = watch.status(engine.state, project)
            if value.get("last_success"):
                break
            time.sleep(0.2)
        assert value.get("last_success"), value
        assert engine.backend.snapshots(project.id)
        previous_id = value["last_success"]["id"]
        file = Path(project.root) / "app.py"
        before = file.stat()
        original = file.read_bytes()
        changed = original.replace(b"saved", b"after")
        assert len(changed) == len(original)
        file.write_bytes(changed)
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            value = watch.status(engine.state, project)
            if value.get("last_success", {}).get("id") != previous_id:
                break
            time.sleep(0.2)
        assert value["last_success"]["id"] != previous_id
    finally:
        watch.stop(engine.state, project)
        deadline = time.monotonic() + 20
        while watch.status(engine.state, project)["running"] and time.monotonic() < deadline:
            time.sleep(0.2)
    assert not watch.status(engine.state, project)["running"]
