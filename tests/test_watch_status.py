import json
import time

from work_rescue import watch
from work_rescue.core import atomic_json


def test_stale_heartbeat_does_not_prevent_requesting_stop(engine, project):
    atomic_json(
        engine.state.home / f"watch-{project.id}.json",
        {
            "running": True,
            "session": "stale-session",
            "heartbeat": time.time() - 30,
        },
    )
    assert not watch.status(engine.state, project)["running"]
    watch.stop(engine.state, project)
    request = json.loads(
        (engine.state.home / f"stop-{project.id}.json").read_text(encoding="utf-8")
    )
    assert request["session"] == "stale-session"
