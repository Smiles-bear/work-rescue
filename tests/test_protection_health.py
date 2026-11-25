import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Static

from work_rescue import core, watch
from work_rescue.core import RescueError, Restic, atomic_json, require_space
from work_rescue.tui import RescueApp


def test_heartbeat_and_save_progress_have_independent_status(engine, project):
    path = engine.state.home / f"watch-{project.id}.json"
    value = {
        "running": True,
        "session": "fixture",
        "heartbeat": time.time(),
        "phase": "capturing",
        "progress_at": time.time() - 150,
        "last_success": {"id": "previous"},
        "consecutive_failures": 2,
    }
    atomic_json(path, value)
    result = watch.status(engine.state, project)
    assert result["running"] and result["activity"] == "stalled"
    assert result["last_success"]["id"] == "previous"
    value.update(phase="waiting", last_error="disk busy")
    atomic_json(path, value)
    assert watch.status(engine.state, project)["activity"] == "retrying"
    value.update(heartbeat=time.time() - 30)
    atomic_json(path, value)
    assert watch.status(engine.state, project)["activity"] == "stopped"
    watch.stop(engine.state, project)
    assert (
        json.loads((engine.state.home / f"stop-{project.id}.json").read_text())["session"]
        == "fixture"
    )


def test_backend_timeout_terminates_a_real_stuck_process(tmp_path, monkeypatch):
    original_run = subprocess.run
    seen = []

    def stuck_command(args, **kwargs):
        seen.append(kwargs["timeout"])
        return original_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(core.subprocess, "run", stuck_command)
    backend = Restic(tmp_path / "repo", "fixture-password", sys.executable, timeout_seconds=0.2)
    started = time.monotonic()
    with pytest.raises(RescueError, match="超过"):
        backend.run("snapshots", "--json")
    assert time.monotonic() - started < 5
    assert seen == [0.2]


def test_failure_then_retry_preserves_success_and_resets_error(engine, project, monkeypatch):
    previous = engine.checkpoint(project)
    original = engine.checkpoint
    status_path = engine.state.home / f"watch-{project.id}.json"
    atomic_json(
        status_path,
        {
            "running": False,
            "heartbeat": 0,
            "last_success": previous,
        },
    )
    calls = 0

    def flaky_checkpoint(project, signature=None, progress=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            progress({"phase": "saving", "completed_files": 0, "total_files": 1})
            raise RescueError("fixture timeout")
        status = watch.status(engine.state, project)
        assert status["consecutive_failures"] == 1 and status["last_error"]
        assert status["last_success"]["id"] == previous["id"]
        result = original(project, signature, progress)
        watch.stop(engine.state, project)
        return result

    real_sleep = time.sleep
    monkeypatch.setattr(engine, "checkpoint", flaky_checkpoint)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: real_sleep(0.001))
    watch.run(engine, project, 5)
    status = watch.status(engine.state, project)
    assert calls == 2 and not status["running"]
    assert status["consecutive_failures"] == 0 and status["last_error"] is None
    assert status["last_checked"] and status["last_success"]["id"]


def test_restore_rejects_low_space_before_extraction(engine, project, tmp_path, monkeypatch):
    saved = engine.checkpoint(project)
    commands = []
    original_run = engine.backend.run

    def record_command(*args, **kwargs):
        commands.append(args[0])
        return original_run(*args, **kwargs)

    monkeypatch.setattr(engine.backend, "run", record_command)
    monkeypatch.setattr(core.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    target = tmp_path / "low-space"
    with pytest.raises(RescueError, match="空间不足"):
        engine.restore(saved["id"], target)
    assert "stats" in commands and "restore" not in commands
    assert not target.exists() and engine.incomplete_restores() == []


def test_space_preflight_combines_copies_on_the_same_volume(tmp_path, monkeypatch):
    other = tmp_path / "staging"
    other.mkdir()
    monkeypatch.setattr(
        core.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024 * 1024 + 150)
    )
    with pytest.raises(RescueError, match="空间不足"):
        require_space([(tmp_path, 100), (other, 100)])


def test_final_corruption_leaves_incomplete_restore_and_allows_retry(
    engine, project, tmp_path, monkeypatch
):
    saved = engine.checkpoint(project)
    original_move = core.shutil.move
    target = tmp_path / "corrupted"

    def damaged_move(source, destination):
        result = original_move(source, destination)
        if Path(destination).name == "app.py":
            path = Path(destination)
            path.write_bytes(path.read_bytes().replace(b"saved", b"wrong"))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(core.shutil, "move", damaged_move)
        with pytest.raises(RescueError, match="恢复未完成"):
            engine.restore(saved["id"], target)
    assert engine.incomplete_restores()[0]["status"] == "incomplete"
    assert b"saved" in (Path(project.root) / "app.py").read_bytes()
    retried = tmp_path / "retried"
    assert engine.restore(saved["id"], retried)["final_files_verified"]
    assert b"saved" in (retried / "app.py").read_bytes()


def test_rehearsal_detects_copy_corruption_and_writes_no_receipt(engine, project, monkeypatch):
    saved = engine.checkpoint(project)
    assert engine.verify(saved["id"])["final_files_verified"]
    original_copy = core.shutil.copy2

    def damaged_copy(source, destination):
        result = original_copy(source, destination)
        if Path(destination).name == "app.py":
            Path(destination).write_bytes(b"bad")
        return result

    monkeypatch.setattr(core.shutil, "copy2", damaged_copy)
    with pytest.raises(RescueError, match="校验失败"):
        engine.verify(saved["id"])
    assert not (engine.state.home / "verification" / f"{saved['id']}.json").exists()


async def test_terminal_shows_stalled_save_and_clears_failed_verification(
    engine, project, monkeypatch
):
    saved = engine.checkpoint(project)
    engine.verify(saved["id"])
    atomic_json(
        engine.state.home / f"watch-{project.id}.json",
        {
            "running": True,
            "heartbeat": time.time(),
            "phase": "capturing",
            "progress_at": time.time() - 150,
            "completed_files": 2,
            "total_files": 4,
            "last_success": saved,
            "consecutive_failures": 1,
        },
    )
    original_copy = core.shutil.copy2

    def damaged_copy(source, destination):
        result = original_copy(source, destination)
        if Path(destination).name == "app.py":
            Path(destination).write_bytes(b"bad")
        return result

    monkeypatch.setattr(core.shutil, "copy2", damaged_copy)
    app = RescueApp(engine)
    async with app.run_test(size=(110, 35)) as pilot:
        await app.workers.wait_for_complete()
        app.update_status()
        text = str(app.query_one("#status", Static).render())
        assert "长时间无新进度" in text and "2/4" in text and "连续失败 1" in text
        snapshots = app.query_one("#snapshots", DataTable)
        assert snapshots.get_cell_at((0, 2)) == "已通过"
        await pilot.press("v")
        await app.workers.wait_for_complete()
        assert snapshots.get_cell_at((0, 2)) == "未演练"
