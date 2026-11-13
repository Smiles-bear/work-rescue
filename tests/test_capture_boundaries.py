from pathlib import Path

import pytest

from work_rescue import core
from work_rescue.core import RescueError, scan


def directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit unprivileged directory links")


def test_replaced_registered_root_is_not_followed(engine, project, tmp_path):
    root = Path(project.root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private content", encoding="utf-8")
    root.rename(tmp_path / "original-root")
    directory_link(root, outside)
    with pytest.raises(RescueError, match="链接"):
        scan(project)
    with pytest.raises(RescueError, match="链接"):
        engine.checkpoint(project)
    assert not engine.backend.snapshots()


def test_ancestor_swap_after_scan_does_not_publish_outside_content(
    engine, project, tmp_path, monkeypatch
):
    root = Path(project.root)
    folder = root / "safe"
    folder.mkdir()
    (folder / "same.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "same.txt").write_text("outside private", encoding="utf-8")
    original_scan = core.scan

    def swapped(project):
        report = original_scan(project)
        folder.rename(tmp_path / "original-safe")
        directory_link(folder, outside)
        return report

    monkeypatch.setattr(core, "scan", swapped)
    with pytest.raises(RescueError, match="链接"):
        engine.checkpoint(project)
    assert not engine.backend.snapshots()
