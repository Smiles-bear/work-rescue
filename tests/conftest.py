import os
import shutil
from pathlib import Path

import pytest

from work_rescue.core import Engine, Restic, State


@pytest.fixture
def engine(tmp_path):
    executable = os.environ.get("WORK_RESCUE_RESTIC") or shutil.which("restic")
    local = (
        Path(__file__).resolve().parents[1]
        / ".tools"
        / ("restic.exe" if os.name == "nt" else "restic")
    )
    if not executable and local.exists():
        executable = str(local)
    if not executable:
        pytest.fail("Install restic or run scripts/bootstrap_restic.py before integration tests")
    return Engine(
        State(tmp_path / "state"), Restic(tmp_path / "repository", "test-only-password", executable)
    )


@pytest.fixture
def project(engine, tmp_path):
    root = tmp_path / "源代码 project"
    root.mkdir()
    (root / "app.py").write_bytes(b"print('saved')\r\n")
    (root / "新 文件.bin").write_bytes(bytes(range(256)))
    (root / ".gitignore").write_text("output/\n", encoding="utf-8")
    (root / "output").mkdir()
    (root / "output" / "report.csv").write_bytes(b"a,b\n1,2\n")
    return engine.state.add("demo", root, ["output/"], "恢复代码和报表")
