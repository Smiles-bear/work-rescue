"""Run the isolated synthetic demo with this checkout's virtual environment."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not interpreter.is_file():
    raise SystemExit("请先按 README 安装开发环境：uv sync --locked --extra dev")
try:
    completed = subprocess.run(
        [str(interpreter), str(root / "scripts/demo.py"), *sys.argv[1:]], check=False
    )
    raise SystemExit(completed.returncode)
except KeyboardInterrupt:
    raise SystemExit(130) from None
