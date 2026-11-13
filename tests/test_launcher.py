import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def invoke(script: Path, *args, cwd: Path):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


def test_launcher_uses_real_cli_and_preserves_paths_with_spaces(tmp_path):
    home = tmp_path / "真实应用 data"
    result = invoke(ROOT / "run.py", "--home", home, "projects", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert home.is_dir()
    assert not (home / "projects.json").exists()
    assert not (tmp_path / ".demo").exists()
    assert not (tmp_path / "示例项目").exists()


def test_launcher_preserves_argument_error_exit_status(tmp_path):
    result = invoke(ROOT / "run.py", "--not-a-real-option", cwd=tmp_path)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not (tmp_path / ".demo").exists()


def test_launcher_can_fall_back_to_installed_current_interpreter(tmp_path):
    launcher = tmp_path / "run.py"
    shutil.copyfile(ROOT / "run.py", launcher)
    result = invoke(launcher, "--help", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Work Rescue" in result.stdout
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / ".demo").exists()
