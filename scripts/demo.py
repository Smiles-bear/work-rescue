"""Create a synthetic project; optionally open the TUI or export its render."""

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

from work_rescue.core import Engine, Restic, State
from work_rescue.tui import RescueApp


async def preview(engine, destination):
    app = RescueApp(engine)
    async with app.run_test(size=(110, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot(destination.name, path=str(destination.parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    demo = root / ".demo" / uuid.uuid4().hex[:8]
    project = demo / "示例项目"
    project.mkdir(parents=True)
    (project / "app.py").write_text("print('first saved version')\n", encoding="utf-8")
    (project / ".gitignore").write_text("output/\n.env\n", encoding="utf-8")
    (project / "output").mkdir()
    (project / "output" / "report.csv").write_text("item,count\n完成任务,3\n", encoding="utf-8")
    (project / ".env").write_text("DEMO_ONLY=not-a-real-secret\n", encoding="utf-8")
    state = State(demo / "state")
    backend = Restic(
        demo / "repository",
        "synthetic-demo-only",
        str(root / ".tools" / ("restic.exe" if os.name == "nt" else "restic")),
    )
    engine = Engine(state, backend)
    record = state.add("示例项目", project, ["output/"], "完成自动化脚本与报表；这是假数据演示")
    engine.checkpoint(record)
    (project / "app.py").write_text("print('second saved version')\n", encoding="utf-8")
    saved = engine.checkpoint(record)
    engine.verify(saved["id"])
    metadata = {
        "home": str(state.home),
        "repository": str(backend.repository),
        "restic": backend.executable,
        "password": "synthetic-demo-only",
    }
    # Only synthetic demonstration credentials are written by this script.
    (root / ".demo" / "latest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.preview:
        destination = root / ".demo" / "preview.svg"
        asyncio.run(preview(engine, destination))
        print(destination)
    else:
        RescueApp(engine).run()


if __name__ == "__main__":
    main()
