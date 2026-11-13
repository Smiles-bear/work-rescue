"""Start Work Rescue for real projects, without creating sample data."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def package_available(interpreter: Path) -> bool:
    """Check the fallback interpreter without starting or configuring the app."""
    try:
        result = subprocess.run(
            [str(interpreter), "-c", "import work_rescue.cli; import textual"],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        interpreter = Path(sys.executable)
        if not package_available(interpreter):
            print(
                "未找到可运行的 Work Rescue 环境。请在工程目录执行："
                "uv sync --locked，然后重新运行。",
                file=sys.stderr,
            )
            return 1
    try:
        completed = subprocess.run(
            [str(interpreter), "-m", "work_rescue", *(sys.argv[1:] if argv is None else argv)],
            check=False,
        )
        return completed.returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"无法启动 Work Rescue：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
