from textual.widgets import DataTable, Input

from work_rescue.tui import RescueApp


async def test_keyboard_add_save_verify_and_resize(engine, tmp_path):
    root = tmp_path / "中文 project"
    root.mkdir()
    (root / "app.py").write_bytes(b"print('hello')\n")
    app = RescueApp(engine)
    async with app.run_test(size=(110, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("a")
        await pilot.pause(2.2)
        app.screen.query_one("#name", Input).value = "中文项目"
        app.screen.query_one("#root", Input).value = str(root)
        await pilot.click("#confirm")
        await app.workers.wait_for_complete()
        assert app.query_one("#projects", DataTable).row_count == 1
        await pilot.click("#save")
        await app.workers.wait_for_complete()
        assert app.query_one("#snapshots", DataTable).row_count == 1
        await pilot.click("#verify")
        await app.workers.wait_for_complete()
        assert list((engine.state.home / "verification").glob("*.json"))
        older = app.selected_snapshot
        (root / "app.py").write_bytes(b"print('newer human work')\n")
        await pilot.click("#save")
        await app.workers.wait_for_complete()
        assert app.selected_snapshot != older
        snapshots = app.query_one("#snapshots", DataTable)
        snapshots.focus()
        snapshots.move_cursor(row=1)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.selected_snapshot == older
        await pilot.click("#verify")
        await app.workers.wait_for_complete()
        assert app.selected_snapshot == older
        assert snapshots.cursor_row == 1
        await pilot.click("#restore")
        await pilot.pause()
        destination = tmp_path / "restored-from-ui"
        app.screen.query_one("#destination", Input).value = str(destination)
        await pilot.click("#confirm")
        await app.workers.wait_for_complete()
        assert (destination / "app.py").read_bytes() == b"print('hello')\n"
        assert (root / "app.py").read_bytes() == b"print('newer human work')\n"
        await pilot.resize_terminal(70, 24)
        await pilot.pause()
        assert app.screen.has_class("narrow")
