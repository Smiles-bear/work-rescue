import pytest

from work_rescue.core import RescueError, State


def test_interval_survives_restart(engine):
    assert engine.state.settings()["interval_seconds"] == 300
    engine.state.set_interval(45)
    assert State(engine.state.home).settings()["interval_seconds"] == 45
    for invalid in (0, 4, 86401):
        with pytest.raises(RescueError):
            engine.state.set_interval(invalid)
    assert engine.state.settings()["interval_seconds"] == 45


def test_project_changes_preserve_identity_and_source_location(engine, project):
    updated = engine.state.update_project(project.id, "真实项目新名", [], "新的目标")
    assert updated.id == project.id
    assert updated.root == project.root
    assert engine.state.get("真实项目新名").goal == "新的目标"
    saved = engine.checkpoint(updated)
    assert "output/report.csv" not in engine.handoff(saved["id"])["files"]
