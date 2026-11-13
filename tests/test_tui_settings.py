from textual.widgets import Input

from work_rescue.core import scan
from work_rescue.tui import RescueApp


async def test_user_changes_real_project_scope_and_interval(engine, project):
    app = RescueApp(engine)
    async with app.run_test(size=(110, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("f2")
        await pilot.pause()
        app.screen.query_one("#interval", Input).value = "60"
        await pilot.click("#confirm")
        await app.workers.wait_for_complete()
        assert engine.state.settings()["interval_seconds"] == 60
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#name", Input).value = "我的实际工程"
        app.screen.query_one("#include", Input).value = ""
        app.screen.query_one("#goal", Input).value = "整理自动化脚本"
        await pilot.click("#confirm")
        await app.workers.wait_for_complete()
        changed = engine.state.get(project.id)
        assert changed.name == "我的实际工程"
        assert changed.root == project.root
        assert "output/report.csv" not in scan(changed)["files"]
