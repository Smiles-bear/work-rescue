from pathlib import Path

import pytest

from work_rescue.core import Project, RescueError
from work_rescue.coverage import scan


def project_at(root, includes=()):
    return Project("coverage-fixture", "coverage", str(root.resolve()), list(includes), "")


def write(root, path, content="fixture"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_ignored_cache_trees_are_not_enumerated(tmp_path, monkeypatch):
    write(tmp_path, ".gitignore", ".tools/\n.demo/\nbuild/\n")
    write(tmp_path, "app.py")
    for folder in (".tools", ".demo", "build"):
        write(tmp_path, f"{folder}/deep/payload.bin")
    original = __import__("os").scandir

    def guarded(path):
        if Path(path).name in (".tools", ".demo", "build"):
            raise AssertionError("An ignored tree was opened")
        return original(path)

    monkeypatch.setattr("work_rescue.coverage.os.scandir", guarded)
    result = scan(project_at(tmp_path))
    assert set(result["files"]) == {".gitignore", "app.py"}
    assert {entry["path"] for entry in result["excluded"]} == {".tools/", ".demo/", "build/"}
    assert any("整棵子树" in note for note in result["notes"])


def test_nested_ignore_can_override_matching_parent_file_rule(tmp_path):
    write(tmp_path, ".gitignore", "*.log\n")
    write(tmp_path, "root.log")
    write(tmp_path, "package/.gitignore", "!keep.log\n/generated/\n")
    write(tmp_path, "package/keep.log")
    write(tmp_path, "package/error.log")
    write(tmp_path, "package/generated/data.txt")
    write(tmp_path, "package/deeper/.gitignore", "!error.log\n")
    write(tmp_path, "package/deeper/error.log")
    result = scan(project_at(tmp_path))
    assert "package/keep.log" in result["files"]
    assert "package/deeper/error.log" in result["files"]
    assert "root.log" not in result["files"]
    assert "package/error.log" not in result["files"]
    assert "package/generated/data.txt" not in result["files"]
    assert any(entry["path"] == "package/generated/" for entry in result["excluded"])


def test_ignored_parent_cannot_be_reincluded_by_child_rule(tmp_path):
    write(tmp_path, ".gitignore", "cache/\n!cache/keep.txt\n")
    write(tmp_path, "cache/.gitignore", "!keep.txt\n")
    write(tmp_path, "cache/keep.txt")
    assert "cache/keep.txt" not in scan(project_at(tmp_path))["files"]


def test_explicit_artifact_selection_does_not_unignore_other_children(tmp_path):
    write(tmp_path, ".gitignore", "output/\n")
    write(tmp_path, "output/.gitignore", "!unrelated.txt\n")
    write(tmp_path, "output/report.csv")
    write(tmp_path, "output/unrelated.txt")
    write(tmp_path, "output/huge-cache/payload.bin")
    result = scan(project_at(tmp_path, ["output/report.csv"]))
    assert "output/report.csv" in result["files"]
    assert "output/unrelated.txt" not in result["files"]
    assert "output/huge-cache/payload.bin" not in result["files"]
    assert any(entry["path"] == "output/huge-cache/" for entry in result["excluded"])


@pytest.mark.parametrize("pattern", ["output/", "output/**/*.csv", "*.csv", "output\\"])
def test_explicit_directory_and_glob_overrides_reach_descendants(tmp_path, pattern):
    write(tmp_path, ".gitignore", "output/\n")
    write(tmp_path, "output/deep/report.csv")
    assert "output/deep/report.csv" in scan(project_at(tmp_path, [pattern]))["files"]


def test_child_negation_works_when_parent_directory_is_reincluded(tmp_path):
    write(tmp_path, ".gitignore", "cache/*\n!cache/keep/\n")
    write(tmp_path, "cache/keep/report.txt")
    write(tmp_path, "cache/drop/report.txt")
    result = scan(project_at(tmp_path))
    assert "cache/keep/report.txt" in result["files"]
    assert "cache/drop/report.txt" not in result["files"]


def test_explicit_selection_never_overrides_credentials_or_dependencies(tmp_path):
    for path in (
        ".env",
        ".env.production",
        "keys/private.pem",
        ".aws/credentials",
        ".npmrc",
        ".pypirc",
        "node_modules/dependency.js",
        ".git/config",
    ):
        write(tmp_path, path, "not a real credential")
    write(tmp_path, ".env.example", "EXAMPLE=value")
    write(tmp_path, "app.py")
    result = scan(project_at(tmp_path, ["**"]))
    assert set(result["files"]) == {".env.example", "app.py"}


def test_external_file_and_directory_links_are_excluded(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = write(tmp_path, "outside/private.txt")
    try:
        (root / "file-link").symlink_to(outside)
        (root / "directory-link").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit unprivileged links")
    result = scan(project_at(root))
    assert result["files"] == []
    assert len(result["excluded"]) == 2


def test_replaced_root_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    project = project_at(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.rename(tmp_path / "original")
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit unprivileged links")
    with pytest.raises(RescueError, match="链接"):
        scan(project)


@pytest.mark.parametrize("pattern", ["../outside", "/absolute", "C:/absolute"])
def test_explicit_patterns_must_be_project_relative(tmp_path, pattern):
    with pytest.raises(RescueError, match="相对路径"):
        scan(project_at(tmp_path, [pattern]))
