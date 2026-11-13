"""Download a pinned upstream restic release, verifying its published SHA-256.

The checksum and binary come from the same release, not an independent signature.
Nothing is installed globally. Run separately on each target OS/architecture.
"""

import bz2
import hashlib
import io
import json
import os
import platform
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.19.1"
ROOT = Path(__file__).resolve().parents[1]


def main():
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}[platform.system()]
    arch = {"AMD64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[
        platform.machine()
    ]
    suffix = "zip" if system == "windows" else "bz2"
    filename = f"restic_{VERSION}_{system}_{arch}.{suffix}"
    base = f"https://github.com/restic/restic/releases/download/v{VERSION}/"
    checksums = urllib.request.urlopen(base + "SHA256SUMS", timeout=90).read().decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.endswith(filename))
    data = urllib.request.urlopen(base + filename, timeout=120).read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError("restic archive checksum mismatch")
    if suffix == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.endswith(".exe")]
            if len(names) != 1:
                raise RuntimeError("Unexpected release archive")
            executable = archive.read(names[0])
    else:
        executable = bz2.decompress(data)
    target = ROOT / ".tools" / ("restic.exe" if system == "windows" else "restic")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(executable)
    if os.name != "nt":
        target.chmod(0o755)
    target.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "url": base + filename,
                "archive_sha256": actual,
                "executable_sha256": hashlib.sha256(executable).hexdigest(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
