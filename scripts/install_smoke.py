"""Exercise a wheel installed into .tools/wheel-venv, not the editable source."""

import json
import os
import subprocess
import uuid
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    python = (
        root / ".tools" / "wheel-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    backend = root / ".tools" / ("restic.exe" if os.name == "nt" else "restic")
    demo = root / ".tools" / ("installed-smoke-" + uuid.uuid4().hex[:8])
    source = demo / "source"
    source.mkdir(parents=True)
    (source / "工作.txt").write_bytes(b"saved bytes\r\n")
    env = os.environ.copy()
    env["WORK_RESCUE_PASSWORD"] = "synthetic-installed-smoke-only"
    env["PYTHONUTF8"] = "1"
    base = [
        str(python),
        "-m",
        "work_rescue",
        "--home",
        str(demo / "state"),
        "--repo",
        str(demo / "repository"),
        "--restic",
        str(backend),
    ]

    def call(*args):
        completed = subprocess.run(
            [*base, *map(str, args)], capture_output=True, env=env, encoding="utf-8", check=True
        )
        return json.loads(completed.stdout)

    installed = subprocess.run(
        [str(python), "-c", "import work_rescue; print(work_rescue.__file__)"],
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=True,
    ).stdout.strip()
    if not Path(installed).is_relative_to(python.parent.parent):
        raise RuntimeError("Smoke test imported editable source instead of installed wheel")
    call("add", "smoke", source)
    saved = call("save", "smoke")
    (source / "工作.txt").write_bytes(b"newer human work\r\n")
    verified = call("verify", saved["id"])
    recovered = demo / "recovered"
    call("restore", saved["id"], recovered)
    if (recovered / "工作.txt").read_bytes() != b"saved bytes\r\n":
        raise RuntimeError("Installed wheel did not restore exact content")
    if (source / "工作.txt").read_bytes() != b"newer human work\r\n":
        raise RuntimeError("Installed wheel changed newer source work")
    result = {
        "installed_module": installed,
        "snapshot_id": saved["id"],
        "rehearsal": verified["rehearsal"],
        "exact_restore": True,
        "original_unchanged": True,
    }
    (root / ".tools" / "installed-smoke.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
