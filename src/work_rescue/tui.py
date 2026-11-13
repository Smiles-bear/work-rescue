from __future__ import annotations

import asyncio
import re
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from . import watch
from .core import Engine, Project, RescueError, scan


def plain(value) -> Text:
    return Text(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "�", str(value)))


class Form(ModalScreen[dict | None]):
    BINDINGS = (("escape", "cancel", "取消"),)
    DEFAULT_CSS = """
    Form { align: center middle; background: $background 70%; }
    Form > VerticalScroll { width: 80%; max-width: 90; max-height: 90%;
        height: auto; border: round $accent; padding: 1 2; background: $surface; }
    Form Input { margin-bottom: 1; }
    Form Horizontal { height: 3; }
    """

    def __init__(self, title: str, fields: list[tuple[str, str]], values: dict | None = None):
        super().__init__()
        self.heading, self.fields = title, fields
        self.values = values or {}

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(self.heading)
            for key, label in self.fields:
                yield Label(label)
                yield Input(value=self.values.get(key, ""), id=key)
            with Horizontal():
                yield Button("确认", id="confirm", variant="primary")
                yield Button("取消", id="cancel")

    def action_cancel(self):
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "confirm":
            self.dismiss({key: self.query_one(f"#{key}", Input).value for key, _ in self.fields})
        else:
            self.dismiss(None)


class RescueApp(App):
    TITLE = "WORK RESCUE · 工作恢复助手"
    SUB_TITLE = "本地加密检查点 / 隔离恢复 / 原会话继续尚未验证"
    CSS = """
    Screen { background: $background; }
    #status { height: auto; min-height: 2; padding: 0 1; background: $panel; }
    #workspace { height: 1fr; }
    #left { width: 30%; min-width: 18; border: round $primary-muted; }
    #right { width: 1fr; border: round $primary-muted; }
    .heading { height: 1; padding: 0 1; color: $accent; text-style: bold; }
    #projects { height: 1fr; }
    #snapshots { height: 1fr; }
    #actions { height: auto; layout: grid; grid-size: 4; grid-rows: 3; }
    #actions Button { width: 1fr; min-width: 8; }
    #details { height: 9; border: round $primary-muted; }
    .narrow #workspace { layout: vertical; }
    .narrow #left { width: 100%; height: 6; }
    .narrow #right { width: 100%; height: 1fr; }
    .narrow #details { height: 3; }
    .narrow #left { height: 3; }
    """
    BINDINGS = (
        ("a", "add_project", "添加"),
        ("s", "save", "保存"),
        ("v", "verify", "验证"),
        ("r", "restore", "恢复"),
        ("e", "edit_project", "编辑项目"),
        ("f2", "settings", "设置"),
        ("ctrl+r", "refresh", "刷新"),
        ("q", "quit", "退出界面"),
    )

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.selected_project: Project | None = None
        self.selected_snapshot: str | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("后台状态：未读取", id="status", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="left"):
                yield Static("项目", classes="heading")
                yield DataTable(id="projects", cursor_type="row")
            with Vertical(id="right"):
                yield Static("检查点 · 选中后验证或恢复", classes="heading")
                yield DataTable(id="snapshots", cursor_type="row")
        with Vertical(id="actions"):
            for key, label in [
                ("add", "添加项目"),
                ("scan", "保护范围"),
                ("save", "保存"),
                ("verify", "恢复演练"),
                ("restore", "恢复"),
                ("diff", "查看差异"),
                ("handoff", "接续资料"),
                ("watch", "开启后台"),
                ("stop", "停止后台"),
                ("refresh", "刷新"),
                ("edit", "编辑项目"),
                ("settings", "设置"),
            ]:
                yield Button(label, id=key, variant="primary" if key == "save" else "default")
        yield RichLog(id="details", wrap=True, markup=False, highlight=False)
        yield Footer()

    async def on_mount(self):
        self.query_one("#projects", DataTable).add_columns("项目")
        self.query_one("#snapshots", DataTable).add_columns("时间", "ID", "演练")
        self.set_interval(2, self.update_status)
        self.query_one("#details", RichLog).write(
            "先添加项目，再查看保护范围。退出界面不会停止独立后台。"
        )
        for operation in self.engine.incomplete_restores():
            self.write_log(f"有未完成的恢复，请保留原项目并选择另一个新目录重试：{operation}")
        self.action_refresh()

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 85, "narrow")

    def update_status(self):
        if self.selected_project:
            value = watch.status(self.engine.state, self.selected_project)
            state = "运行中" if value["running"] else "未运行"
            message = f"{self.selected_project.name}  |  独立后台：{state}"
            if value.get("last_success"):
                message += f"  |  最近自动保存：{value['last_success']['completed_at']}"
            if value.get("last_error"):
                message += f"  |  保存失败：{value['last_error']}"
        else:
            message = "请选择项目；尚未开启自动保护。原生聊天继续：未验证。"
        if self.busy:
            message = "操作进行中  |  " + message
        self.query_one("#status", Static).update(plain(message))

    def write_log(self, message):
        self.query_one("#details", RichLog).write(plain(message))

    async def perform(self, function, *args):
        if self.busy:
            self.write_log("已有操作运行中，请稍候。")
            return None
        self.busy = True
        self.query_one("#status", Static).update("操作进行中；界面仍可浏览。")
        try:
            return await asyncio.to_thread(function, *args)
        except (RescueError, OSError, ValueError) as exc:
            self.write_log(f"失败：{exc}")
            return None
        finally:
            self.busy = False
            self.update_status()

    @work(exclusive=True, group="refresh")
    async def action_refresh(self):
        projects = self.engine.state.projects()
        table = self.query_one("#projects", DataTable)
        table.clear()
        for project in projects:
            table.add_row(plain(project.name), key=project.id)
        if not self.selected_project and projects:
            self.selected_project = projects[0]
        if self.selected_project:
            await self.load_snapshots()
        self.update_status()

    async def load_snapshots(self):
        project = self.selected_project
        if project is None:
            return
        records = await self.perform(self.engine.backend.snapshots, project.id)
        if records is None or project != self.selected_project:
            return
        previous = self.selected_snapshot
        table = self.query_one("#snapshots", DataTable)
        table.clear()
        for record in records:
            verified = (self.engine.state.home / "verification" / f"{record['id']}.json").exists()
            table.add_row(
                plain(record["time"][:19]),
                record["id"][:8],
                "已通过" if verified else "未演练",
                key=record["id"],
            )
        identifiers = [record["id"] for record in records]
        self.selected_snapshot = (
            previous if previous in identifiers else (identifiers[0] if identifiers else None)
        )
        if self.selected_snapshot:
            table.move_cursor(row=identifiers.index(self.selected_snapshot))

    @work(group="select")
    async def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id == "projects":
            self.selected_project = self.engine.state.get(str(event.row_key.value))
            self.selected_snapshot = None
            await self.load_snapshots()
            self.update_status()
        else:
            self.selected_snapshot = str(event.row_key.value)
            self.write_log(f"已选择检查点：{self.selected_snapshot[:12]}")

    @work(group="operation")
    async def action_add_project(self):
        data = await self.push_screen_wait(
            Form(
                "添加项目",
                [
                    ("name", "项目名称"),
                    ("root", "项目完整路径"),
                    ("include", "需要保护的忽略产物，例如 output/（逗号分隔）"),
                    ("goal", "任务目标（可选）"),
                ],
            )
        )
        if data:
            if not data["root"].strip() or not Path(data["root"]).expanduser().is_absolute():
                self.write_log("请填写需要保护的项目完整路径。")
                return
            result = await self.perform(
                self.engine.state.add,
                data["name"],
                Path(data["root"]),
                [x.strip() for x in data["include"].split(",") if x.strip()],
                data["goal"],
            )
            if result:
                self.selected_project = result
                self.write_log("项目已添加；请先查看保护范围。")
                self.action_refresh()

    @work(group="operation")
    async def action_edit_project(self):
        project = self.selected_project
        if not project:
            self.write_log("请选择项目。")
            return
        if watch.status(self.engine.state, project)["running"]:
            self.write_log("请先停止该项目后台，再修改保护范围。")
            return
        data = await self.push_screen_wait(
            Form(
                "编辑项目 · 路径保持不变",
                [
                    ("name", "项目名称"),
                    ("include", "包含的忽略产物（逗号分隔）"),
                    ("goal", "任务目标"),
                ],
                {
                    "name": project.name,
                    "include": ",".join(project.include_ignored),
                    "goal": project.goal,
                },
            )
        )
        if data:
            updated = await self.perform(
                self.engine.state.update_project,
                project.id,
                data["name"],
                [s.strip() for s in data["include"].split(",") if s.strip()],
                data["goal"],
            )
            if updated:
                self.selected_project = updated
                self.write_log("项目设置已保存；下一次保存采用新的保护范围。")
                self.action_refresh()

    @work(group="operation")
    async def action_settings(self):
        settings = self.engine.state.settings()
        self.write_log(
            f"应用数据：{self.engine.state.home}\n备份位置：{self.engine.backend.repository}"
        )
        data = await self.push_screen_wait(
            Form(
                "自动保存设置",
                [("interval", "检查变化的间隔（秒，5—86400）")],
                {"interval": str(settings["interval_seconds"])},
            )
        )
        if data:
            try:
                seconds = int(data["interval"])
            except ValueError:
                self.write_log("间隔需要填写整数秒。")
                return
            result = await self.perform(self.engine.state.set_interval, seconds)
            if result:
                self.write_log(f"间隔已设为 {seconds} 秒；下次开启后台时生效。")

    @work(group="operation")
    async def action_save(self):
        if not self.selected_project:
            self.write_log("请先添加或选择项目。")
            return
        result = await self.perform(self.engine.checkpoint, self.selected_project)
        if result:
            self.selected_snapshot = result["id"]
            self.write_log(
                f"已保存 {result['file_count']} 个文件；检查点 {result['id'][:12]}。尚未演练。"
            )
            await self.load_snapshots()

    @work(group="operation")
    async def action_verify(self):
        if not self.selected_snapshot:
            self.write_log("请选择检查点。")
            return
        result = await self.perform(self.engine.verify, self.selected_snapshot)
        if result:
            self.write_log(f"恢复演练通过：{result['file_count']} 个文件。原生会话继续未验证。")
            await self.load_snapshots()

    @work(group="operation")
    async def action_restore(self):
        snapshot = self.selected_snapshot
        if not snapshot:
            self.write_log("请选择检查点。")
            return
        data = await self.push_screen_wait(
            Form(
                f"恢复 {snapshot[:12]}：仅写入新目录",
                [("destination", "新目录完整路径（必须不存在，上级目录必须存在）")],
            )
        )
        if data and data["destination"]:
            result = await self.perform(self.engine.restore, snapshot, Path(data["destination"]))
            if result:
                self.write_log(f"恢复完成：{result['destination']}（{result['files']} 个文件）")

    @work(group="operation")
    async def on_button_pressed(self, event: Button.Pressed):
        key = event.button.id
        if key in ("add", "save", "verify", "restore", "refresh", "edit", "settings"):
            action = {"add": "add_project", "edit": "edit_project"}.get(key, key)
            getattr(self, "action_" + action)()
        elif self.selected_project:
            if key == "scan":
                result = await self.perform(scan, self.selected_project)
                if result:
                    self.write_log(
                        f"项目：{self.selected_project.root}\n保护 {len(result['files'])} 个文件；排除 {len(result['excluded'])} 项。"
                    )
                    for path in result["files"]:
                        self.write_log(f"  + {path}")
                    for item in result["excluded"]:
                        self.write_log(f"  - {item['path']}：{item['reason']}")
                    for note in result["notes"]:
                        self.write_log(f"说明：{note}")
            elif key == "watch":
                interval = self.engine.state.settings()["interval_seconds"]
                result = await self.perform(
                    watch.start, self.engine, self.selected_project, interval
                )
                if result:
                    self.write_log(
                        f"独立后台已启动，每 {interval} 秒检查变化。退出界面后继续运行。"
                    )
            elif key == "stop":
                watch.stop(self.engine.state, self.selected_project)
                self.write_log("已请求停止后台；正在进行的保存结束后停止。")
            elif key in ("diff", "handoff"):
                if not self.selected_snapshot:
                    self.write_log("请选择检查点。")
                    return
                if key == "diff":
                    result = await self.perform(
                        self.engine.diff, self.selected_snapshot, self.selected_project
                    )
                else:
                    result = await self.perform(self.engine.handoff, self.selected_snapshot)
                if result:
                    if key == "diff":
                        for label, field in [
                            ("当前已改动", "changed_since_checkpoint"),
                            ("当前已缺失", "missing_now"),
                            ("检查点之后新增", "new_since_checkpoint"),
                            ("数据库需要单独比较", "sqlite_needs_semantic_comparison"),
                        ]:
                            self.write_log(f"{label}：{len(result[field])} 项")
                            for path in result[field]:
                                self.write_log(f"  {path}")
                    else:
                        self.write_log(
                            f"任务目标：{result['goal_confirmed_by_user'] or '未填写'}\n保存时间：{result['captured_at']}"
                        )
                        for path in result["files"]:
                            self.write_log(f"  {path}")
                        self.write_log(
                            "恢复目录内的测试：尚未执行；外部操作状态：未知；原生会话继续：未验证。"
                        )
        else:
            self.write_log("请先添加或选择项目。")
