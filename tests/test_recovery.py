import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from work_rescue import core
from work_rescue.core import Engine, RescueError, State, digest, scan, validate_paths


def file_hashes(root):
    return {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()}


def test_real_encrypted_roundtrip_keeps_newer_work(engine, project, tmp_path):
    source = Path(project.root)
    original = file_hashes(source)
    saved = engine.checkpoint(project)
    (source / "app.py").write_text("newer human edit", encoding="utf-8")
    (source / "new.py").write_text("new work", encoding="utf-8")
    before_restore = file_hashes(source)
    result = engine.restore(saved["id"], tmp_path / "recovered")
    assert file_hashes(tmp_path / "recovered") == original
    assert file_hashes(source) == before_restore
    assert result["native_session"] == "not_verified"


def test_verification_is_repeatable_and_rehearses_real_restore(engine, project):
    saved = engine.checkpoint(project)
    before = file_hashes(engine.backend.repository)
    for _ in range(10):
        result = engine.verify(saved["id"])
        assert result["rehearsal"] == "passed"
    assert before == file_hashes(engine.backend.repository)


def test_ignored_outputs_and_credentials_are_explicit(project):
    root = Path(project.root)
    (root / ".env").write_text("SECRET=test-only", encoding="utf-8")
    result = scan(replace(project, include_ignored=[]))
    assert "output/report.csv" not in result["files"]
    assert {r["path"] for r in result["excluded"]} >= {".env", "output/"}
    included = scan(project)
    assert "output/report.csv" in included["files"]
    assert ".env" not in included["files"]


def test_interrupted_capture_does_not_publish_partial_checkpoint(engine, project, monkeypatch):
    previous = engine.checkpoint(project)
    original_copy = core.stable_copy
    calls = 0

    def interrupted(source, target):
        nonlocal calls
        calls += 1
        original_copy(source, target)
        if calls == 2:
            raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(core, "stable_copy", interrupted)
        with pytest.raises(OSError, match="simulated"):
            engine.checkpoint(project)
    assert [s["id"] for s in engine.backend.snapshots(project.id)] == [previous["id"]]
    assert engine.verify(previous["id"])["rehearsal"] == "passed"


def test_existing_and_source_targets_are_rejected(engine, project, tmp_path):
    saved = engine.checkpoint(project)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    for destination in (
        existing,
        Path(project.root) / "nested",
        engine.backend.repository / "nested",
    ):
        with pytest.raises(RescueError):
            engine.restore(saved["id"], destination)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_source_and_git_metadata_unchanged(engine, project, tmp_path):
    root = Path(project.root)
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "app.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    original = file_hashes(root)
    saved = engine.checkpoint(project)
    engine.restore(saved["id"], tmp_path / "restored")
    assert file_hashes(root) == original
    assert not (tmp_path / "restored" / ".git").exists()


def test_active_wal_database_is_captured_without_touching_original(engine, project, tmp_path):
    database = Path(project.root) / "state.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE work (value TEXT)")
        connection.execute("INSERT INTO work VALUES ('committed in WAL')")
        connection.commit()
        before = file_hashes(Path(project.root))
        saved = engine.checkpoint(project)
        assert file_hashes(Path(project.root)) == before
        target = tmp_path / "restored"
        engine.restore(saved["id"], target)
        with closing(
            sqlite3.connect(f"{(target / 'state.sqlite').as_uri()}?immutable=1", uri=True)
        ) as restored:
            assert restored.execute("SELECT value FROM work").fetchall() == [("committed in WAL",)]
        assert not (target / "state.sqlite-wal").exists()
        assert not (target / "state.sqlite-shm").exists()
    finally:
        connection.close()


def test_recover_without_original_local_configuration(engine, project, tmp_path):
    saved = engine.checkpoint(project)
    fresh = Engine(State(tmp_path / "fresh-state"), engine.backend)
    result = fresh.restore(saved["id"], tmp_path / "new-machine-recovery")
    assert result["files"] == 4
    assert not fresh.state.projects()


def test_symlink_outside_project_is_not_followed(engine, project, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("private", encoding="utf-8")
    link = Path(project.root) / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Host does not permit unprivileged symlinks")
    assert "link.txt" not in scan(project)["files"]
    assert any(r["path"] == "link.txt" for r in scan(project)["excluded"])


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "a/../../b", "a\\b", "a//b", "C:/file"]
)
def test_unsafe_manifest_paths_are_rejected(name):
    with pytest.raises(RescueError):
        validate_paths([name], windows=False, insensitive=False)


@pytest.mark.parametrize(
    "names",
    [
        ["A.py", "a.py"],
        ["é.txt", "e\u0301.txt"],
        ["file", "file/child"],
        ["Dir", "dir/child"],
        ["é", "e\u0301/child"],
    ],
)
def test_cross_platform_path_collisions_are_rejected(names):
    with pytest.raises(RescueError):
        validate_paths(names, windows=True, insensitive=True)


@pytest.mark.parametrize("name", ["CON.txt", "aux", "bad?.txt", "trailing."])
def test_windows_incompatible_paths_are_rejected(name):
    with pytest.raises(RescueError):
        validate_paths([name], windows=True)


def test_wrong_password_does_not_create_a_recovery(engine, project, tmp_path):
    saved = engine.checkpoint(project)
    engine.backend.password = "incorrect-password"
    with pytest.raises(RescueError):
        engine.restore(saved["id"], tmp_path / "recovered")
    assert not (tmp_path / "recovered").exists()


def test_content_based_checkpoints_do_not_miss_preserved_timestamps(engine, project):
    saved = engine.checkpoint(project)
    unchanged = engine.checkpoint(project, saved["signature"])
    assert unchanged["unchanged"]
    assert len(engine.backend.snapshots(project.id)) == 1
    file = Path(project.root) / "app.py"
    info = file.stat()
    file.write_bytes(file.read_bytes().replace(b"saved", b"after"))
    os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
    updated = engine.checkpoint(project, saved["signature"])
    assert updated["id"] != saved["id"]


def test_diff_and_handoff_do_not_claim_tests_were_run(engine, project):
    saved = engine.checkpoint(project)
    root = Path(project.root)
    (root / "app.py").write_text("later work", encoding="utf-8")
    (root / "new.py").write_text("new work", encoding="utf-8")
    result = engine.diff(saved["id"], project)
    assert result["changed_since_checkpoint"] == ["app.py"]
    assert result["new_since_checkpoint"] == ["new.py"]
    handoff = engine.handoff(saved["id"])
    assert handoff["goal_confirmed_by_user"] == project.goal
    assert handoff["tests_in_recovered_directory"] == "not_run"
    assert handoff["external_side_effects"] == "unknown"


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute permission")
def test_executable_mode_survives_restore(engine, project, tmp_path):
    script = Path(project.root) / "run.sh"
    script.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    script.chmod(0o755)
    saved = engine.checkpoint(project)
    engine.restore(saved["id"], tmp_path / "recovered")
    assert (tmp_path / "recovered" / "run.sh").stat().st_mode & 0o777 == 0o755
