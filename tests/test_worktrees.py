import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from work_rescue import core, watch
from work_rescue.core import Engine, RescueError, State, WorktreeChanged, scan
from work_rescue.git_worktrees import discover, parse_worktrees
from work_rescue.tui import RescueApp, WorktreeScreen


def git(path, *args, check=True, env=None):
    return subprocess.run(
        ["git", "-C", str(path), *map(str, args)],
        capture_output=True,
        check=check,
        timeout=15,
        env=env,
    )


@pytest.fixture
def worktrees(engine, tmp_path):
    main = tmp_path / "source"
    main.mkdir()
    git(main, "init", "-b", "main")
    (main / "app.py").write_bytes(b"original\n")
    git(main, "add", "app.py")
    git(
        main,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    checkout = tmp_path / "任务 checkout"
    git(main, "worktree", "add", "--detach", checkout, "HEAD")
    (main / "app.py").write_bytes(b"main changes\n")
    (checkout / "app.py").write_bytes(b"checkout changes\n")
    (checkout / "notes.txt").write_bytes(b"untracked notes\n")
    (checkout / ".gitignore").write_bytes(b"ignored.txt\n")
    return engine.state.add("main", main, [], ""), checkout


def test_old_pointer_is_verified_but_never_published(engine, worktrees, tmp_path, monkeypatch):
    _, checkout = worktrees
    project = engine.state.add("old checkout", checkout, [], "")
    pointer = (checkout / ".git").read_bytes()
    nested = checkout / "nested"
    nested.mkdir()
    (nested / ".GIT").write_bytes(pointer)
    assert set(scan(project)["files"]) == {".gitignore", "app.py", "notes.txt"}
    with monkeypatch.context() as patch:

        def legacy_scan(project):
            result = scan(project)
            result["files"] += [".git", "nested/.GIT"]
            return result

        patch.setattr(core, "scan", legacy_scan)
        snapshot = engine.checkpoint(project)["id"]
    receipt = engine.verify(snapshot)
    assert receipt["file_count"] == 5
    assert receipt["recoverable_file_count"] == 3
    assert receipt["excluded_git_metadata_count"] == 2
    target = tmp_path / "recovered-legacy"
    assert engine.restore(snapshot, target)["files"] == 3
    assert not (target / ".git").exists()
    assert not (target / "nested").exists()
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert git(target, "rev-parse", "--absolute-git-dir", check=False, env=env).returncode != 0
    assert (checkout / ".git").read_bytes() == pointer
    assert engine.handoff(snapshot)["files"] == [".gitignore", "app.py", "notes.txt"]
    assert engine.diff(snapshot, project)["missing_now"] == []

    original_digest = core.digest
    monkeypatch.setattr(core, "digest", lambda p: "bad" if p.name == ".git" else original_digest(p))
    with pytest.raises(RescueError, match=r"\.git"):
        engine.restore(snapshot, tmp_path / "corrupt")
    assert not (tmp_path / "corrupt").exists()


def test_discover_save_remove_and_replace(engine, worktrees, tmp_path):
    main, checkout = worktrees
    main_path = Path(main.root)
    git(main_path, "worktree", "lock", "--reason", "test lock", checkout)
    listing = discover(engine.state, main)
    row = listing["worktrees"][1]
    assert Path(row["path"]) == checkout and row["detached"] and row["locked"] == "test lock"
    assert row["coverage"] == "unregistered"
    linked = engine.state.register_worktree(main.id, checkout, "linked", [".git"], "")
    assert engine.state.get(linked.id) == linked
    first = engine.checkpoint(main)["id"]
    second = engine.checkpoint(linked)["id"]
    assert [row["id"] for row in engine.backend.snapshots(main.id)] == [first]
    assert [row["id"] for row in engine.backend.snapshots(linked.id)] == [second]
    fresh = Engine(State(tmp_path / "new-state"), engine.backend)
    with pytest.raises(RescueError, match="工作树"):
        fresh.restore(second, main_path / ".git" / "objects" / "recovered")
    with pytest.raises(RescueError, match="工作树"):
        fresh.restore(second, main_path / "recovered")
    git(main_path, "worktree", "unlock", checkout)
    git(main_path, "worktree", "remove", "--force", checkout)
    absent = discover(engine.state, main)["worktrees"][1]
    assert Path(absent["path"]) == checkout and absent["directory"] == "not_listed"
    assert absent["project_id"] == linked.id
    with pytest.raises(WorktreeChanged):
        engine.checkpoint(linked)
    assert engine.state.get(linked.id).worktree.paused
    git(main_path, "worktree", "add", "--detach", checkout, "HEAD")
    with pytest.raises(WorktreeChanged):
        engine.checkpoint(linked)
    replacement = engine.state.register_worktree(main.id, checkout, "new checkout", [], "", True)
    assert replacement.id != linked.id and engine.state.get(linked.id).worktree.history_only
    for snapshot, name, expected in (
        (first, "recover-main", b"main changes\n"),
        (second, "recover-linked", b"checkout changes\n"),
    ):
        target = tmp_path / name
        engine.restore(snapshot, target)
        assert (target / "app.py").read_bytes() == expected
        assert not (target / ".git").exists()
        assert (target / "notes.txt").exists() == (snapshot == second)
    assert engine.verify(second)["rehearsal"] == "passed"


def test_discovery_from_subdirectory_and_without_password(engine, worktrees, monkeypatch, capsys):
    from work_rescue.cli import main as cli

    main, checkout = worktrees
    nested = checkout / "selected"
    nested.mkdir()
    part = engine.state.add("partial", nested, [], "")
    monkeypatch.delenv("WORK_RESCUE_PASSWORD", raising=False)
    monkeypatch.setenv("GIT_DIR", str(Path(main.root) / ".git"))
    cli(["--home", str(engine.state.home), "worktrees", part.id])
    rows = json.loads(capsys.readouterr().out)["worktrees"]
    assert rows[1]["partial_projects"] == [part.name]
    assert rows[1]["project_id"] is None


def test_plain_subdirectory_can_restore_to_sibling(engine, worktrees):
    _, checkout = worktrees
    nested = checkout / "component"
    nested.mkdir()
    (nested / "work.txt").write_bytes(b"scoped work")
    project = engine.state.add("scoped", nested, [], "")
    saved = engine.checkpoint(project)
    sibling = checkout / "recovered-component"
    engine.restore(saved["id"], sibling)
    assert (sibling / "work.txt").read_bytes() == b"scoped work"


def test_background_stops_on_removed_worktree(engine, worktrees):
    main, checkout = worktrees
    project = engine.state.register_worktree(main.id, checkout, "watched", [], "")
    try:
        watch.start(engine, project, interval=5)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            first = watch.status(engine.state, project).get("last_success")
            if first:
                break
            time.sleep(0.15)
        assert first
        git(main.root, "worktree", "remove", "--force", checkout)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = watch.status(engine.state, project)
            if not value["running"]:
                break
            time.sleep(0.15)
        assert not value["running"] and value["last_error"]
        assert value["last_success"]["id"] == first["id"]
        assert engine.state.get(project.id).worktree.paused
        assert [record["id"] for record in engine.backend.snapshots(project.id)] == [first["id"]]
    finally:
        watch.stop(engine.state, project)


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline in file names")
def test_nul_parser_preserves_newline_in_worktree_path(tmp_path):
    path = tmp_path / "first line\nsecond line"
    path.mkdir()
    result = parse_worktrees(b"worktree " + os.fsencode(path) + b"\x00detached\x00\x00")
    assert result[0]["path"] == str(path)


async def test_tui_register_verify_and_restore_worktree(engine, worktrees, tmp_path):
    _, checkout = worktrees
    app = RescueApp(engine)
    async with app.run_test(size=(110, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, WorktreeScreen)
        async with asyncio.timeout(15):
            while not app.screen.rows:
                await pilot.pause(0.05)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        table.move_cursor(row=1)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#name", Input).value == checkout.name
        await pilot.click("#confirm")
        async with asyncio.timeout(20):
            await app.workers.wait_for_complete()
        assert Path(app.selected_project.root) == checkout
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.press("v")
        await app.workers.wait_for_complete()
        await pilot.press("r")
        await pilot.pause()
        destination = tmp_path / "worktree-from-ui"
        app.screen.query_one("#destination", Input).value = str(destination)
        await pilot.click("#confirm")
        await app.workers.wait_for_complete()
        assert (destination / "notes.txt").read_bytes() == b"untracked notes\n"
        assert not (destination / ".git").exists()
