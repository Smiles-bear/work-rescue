from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from . import watch
from .core import Engine, Project, RescueError, scan
from .git_worktrees import discover


def plain(value) -> Text:
    return Text(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "�", str(value)))


def tree_text(value: str) -> str:
    return "".join(
        char if unicodedata.category(char) not in ("Cc", "Cf") else ascii(char)[1:-1]
        for char in value
    )


def verification_label(engine: Engine, snapshot: str) -> str:
    receipt = engine.state.home / "verification" / f"{snapshot}.json"
    try:
        value = json.loads(receipt.read_text(encoding="utf-8"))
        if value.get("snapshot_id") == snapshot and value.get("rehearsal") == "passed":
            return "已通过" if "recoverable_file_count" in value else "旧版验证"
    except (OSError, ValueError, AttributeError):
        pass
    return "未演练"


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
            yield Label(self.heading, markup=False)
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


class WorktreeScreen(ModalScreen[tuple[str, dict] | None]):
    BINDINGS = (("escape", "close", "返回"),)
    DEFAULT_CSS = """
    WorktreeScreen { align: center middle; background: $background 70%; }
    WorktreeScreen > Vertical { width: 96%; height: 88%; border: round $accent;
        padding: 0 1; background: $surface; }
    WorktreeScreen DataTable { height: 1fr; }
    WorktreeScreen #tree-info { height: auto; max-height: 5; }
    WorktreeScreen Horizontal { height: 3; }
    WorktreeScreen Button { width: 1fr; min-width: 9; }
    """

    def __init__(self, engine: Engine, source: Project):
        super().__init__()
        self.engine, self.source = engine, source
        self.rows = []
        self.discovery_error = None
        self.latest = None

    def compose(self):
        with Vertical():
            yield Label(f"工作树 · {self.source.name}", markup=False)
            yield DataTable(id="worktree-list", cursor_type="row")
            yield Static("正在查找工作树…", id="tree-info", markup=False)
            with Horizontal():
                yield Button("打开项目", id="tree-open", disabled=True)
                yield Button("登记", id="tree-register", disabled=True)
                yield Button("刷新", id="tree-refresh")
                yield Button("返回", id="tree-close")

    def on_mount(self):
        table = self.query_one(DataTable)
        for label, width in (
            ("路径", 30),
            ("分支", 12),
            ("保护", 16),
            ("最近保存", 19),
            ("演练", 9),
        ):
            table.add_column(label, width=width)
        self.refresh_worktrees()

    def collect(self):
        listing = discover(self.engine.state, self.source)
        latest = {}
        try:
            if any(row["project_id"] for row in listing["worktrees"]):
                for record in self.engine.backend.snapshots():
                    for tag in record.get("tags", []):
                        if tag.startswith("wr:project:"):
                            latest.setdefault(tag.removeprefix("wr:project:"), record)
        except (RescueError, OSError, ValueError):
            return listing, None
        return listing, latest

    @work(exclusive=True, group="worktree-discovery")
    async def refresh_worktrees(self):
        self.query_one("#tree-info", Static).update("正在查找工作树…")
        try:
            listing, latest = await asyncio.to_thread(self.collect)
        except (RescueError, OSError, ValueError) as exc:
            listing, latest = {"worktrees": [], "error": str(exc)}, None
        table = self.query_one(DataTable)
        selected = self.rows[table.cursor_row]["path"] if self.rows else None
        self.rows = listing["worktrees"]
        self.discovery_error = listing["error"]
        self.latest = latest
        table.clear()
        for index, row in enumerate(self.rows):
            record = latest.get(row["project_id"]) if latest is not None else None
            saved = record["time"][:19] if record else "未知" if latest is None else "—"
            coverage = {
                "bound": "已登记",
                "registered": "已登记目录",
                "paused": "已暂停",
                "unregistered": "仅保护子目录" if row["partial_projects"] else "未登记",
            }[row["coverage"]]
            if row["project_id"]:
                project = self.engine.state.get(row["project_id"])
                if watch.status(self.engine.state, project)["running"]:
                    coverage += " · 后台运行"
            branch = "detached" if row["detached"] else (row["branch"] or "—")
            table.add_row(
                plain(tree_text(row["path"])),
                plain(tree_text(branch.removeprefix("refs/heads/"))),
                coverage,
                saved,
                verification_label(self.engine, record["id"]) if record else "—",
            )
            if selected == row["path"]:
                table.move_cursor(row=index)
        if self.rows:
            self.show_row(table.cursor_row)
            table.focus()
        else:
            self.query_one("#tree-open", Button).disabled = True
            self.query_one("#tree-register", Button).disabled = True
            self.query_one("#tree-info", Static).update(plain(listing["error"] or "没有工作树"))

    def show_row(self, index):
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        directory = {
            "available": "目录可用",
            "missing": "目录已缺失",
            "bare": "裸仓库",
            "unavailable": "目录不可用",
            "unsupported_path": "文件名无法表示",
            "not_listed": "Git 列表中无此目录，可查看已保存历史",
        }
        details = [directory[row["directory"]], tree_text(row["path"])]
        if self.latest is not None and (record := self.latest.get(row["project_id"])):
            details.append(f"最近检查点 {record['id'][:12]}")
        if row["partial_projects"]:
            details.append("子目录项目 " + tree_text("、".join(row["partial_projects"])))
        if self.discovery_error:
            details.append(tree_text(self.discovery_error))
        for key, label in (("locked", "Git 已锁定"), ("prunable", "Git 记录可清理")):
            if row[key] is not None:
                details.append(f"{label} {tree_text(row[key])}".strip())
        self.query_one("#tree-info", Static).update(plain("  |  ".join(details)))
        self.query_one("#tree-open", Button).disabled = not row["project_id"]
        button = self.query_one("#tree-register", Button)
        button.disabled = row["directory"] != "available" or row["coverage"] == "bound"
        button.label = {"paused": "登记替换目录", "registered": "绑定已有项目"}.get(
            row["coverage"], "登记"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        self.show_row(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        event.stop()
        row = self.rows[event.cursor_row]
        if row["project_id"]:
            self.dismiss(("open", row))
        elif row["directory"] == "available":
            self.dismiss(("register", row))

    def action_close(self):
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        action = event.button.id
        if action == "tree-close":
            self.action_close()
        elif action == "tree-refresh":
            self.refresh_worktrees()
        elif self.rows:
            row = self.rows[self.query_one(DataTable).cursor_row]
            self.dismiss(("open" if action == "tree-open" else "register", row))


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
    #actions { height: auto; layout: grid; grid-size: 5; grid-rows: 3; }
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
        ("w", "worktrees", "工作树"),
        ("f2", "settings", "设置"),
        ("ctrl+r", "refresh", "刷新"),
        ("q", "quit", "退出界面"),
    )

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.selected_project: Project | None = None
        self.selected_snapshot: str | None = None
        self.snapshot_project_id: str | None = None
        self.snapshot_load = 0
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
                ("worktrees", "工作树"),
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
            if self.selected_project.worktree and not Path(self.selected_project.root).is_dir():
                message += "  |  工作树目录不可用，可恢复已保存的检查点"
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
            label = project.name
            if project.worktree:
                label += " · 历史" if project.worktree.history_only else " · 工作树"
            table.add_row(plain(label), key=project.id)
        selected = self.selected_project.id if self.selected_project else None
        self.selected_project = next((p for p in projects if p.id == selected), None)
        if projects:
            self.selected_project = self.selected_project or projects[0]
            table.move_cursor(row=projects.index(self.selected_project))
        if self.selected_project:
            await self.load_snapshots()
        self.update_status()

    async def load_snapshots(self):
        project = self.selected_project
        if project is None:
            return
        table = self.query_one("#snapshots", DataTable)
        if self.snapshot_project_id != project.id:
            self.snapshot_project_id = project.id
            table.clear()
        self.snapshot_load += 1
        request = self.snapshot_load
        try:
            records = await asyncio.to_thread(self.engine.backend.snapshots, project.id)
        except (RescueError, OSError, ValueError) as exc:
            if request == self.snapshot_load:
                self.write_log(f"无法读取检查点：{exc}")
            return
        if request != self.snapshot_load or project != self.selected_project:
            return
        previous = self.selected_snapshot
        table.clear()
        for record in records:
            table.add_row(
                plain(record["time"][:19]),
                record["id"][:8],
                verification_label(self.engine, record["id"]),
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
        elif event.data_table.id == "snapshots":
            self.selected_snapshot = str(event.row_key.value)
            self.write_log(f"已选择检查点：{self.selected_snapshot[:12]}")

    @work(group="operation")
    async def action_worktrees(self):
        source = self.selected_project
        if not source:
            self.write_log("请先添加或选择项目。")
            return
        choice = await self.push_screen_wait(WorktreeScreen(self.engine, source))
        if not choice:
            return
        action, row = choice
        if action == "open":
            self.selected_project = self.engine.state.get(row["project_id"])
        else:
            root = Path(row["path"])
            if row["coverage"] == "registered":
                data = await self.push_screen_wait(Form(f"绑定已有项目 {tree_text(str(root))}", []))
                if data is not None:
                    data = {"name": row["project_name"], "include": "", "goal": ""}
            else:
                title = "登记新工作树"
                if row["coverage"] == "paused":
                    title = "登记新工作树并保留旧项目的恢复历史"
                data = await self.push_screen_wait(
                    Form(
                        f"{title} · {tree_text(str(root))}",
                        [
                            ("name", "名称"),
                            ("include", "显式包含的忽略产物（逗号分隔）"),
                            ("goal", "任务目标（可选）"),
                        ],
                        {"name": tree_text(root.name)},
                    )
                )
            if data is None:
                return
            project = await self.perform(
                self.engine.state.register_worktree,
                source.id,
                root,
                data["name"],
                [s.strip() for s in data["include"].split(",") if s.strip()],
                data["goal"],
                row["coverage"] == "paused",
            )
            if not project:
                return
            self.selected_project = project
            self.write_log("工作树已登记。可查看保护范围、保存或开启独立后台。")
        self.selected_snapshot = None
        self.action_refresh()

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
        project = self.selected_project
        if not project:
            self.write_log("请先添加或选择项目。")
            return
        result = await self.perform(self.engine.checkpoint, project)
        if result:
            self.write_log(
                f"已保存 {result['file_count']} 个文件；检查点 {result['id'][:12]}。尚未演练。"
            )
            if self.selected_project and self.selected_project.id == project.id:
                self.selected_snapshot = result["id"]
                await self.load_snapshots()

    @work(group="operation")
    async def action_verify(self):
        if not self.selected_snapshot:
            self.write_log("请选择检查点。")
            return
        result = await self.perform(self.engine.verify, self.selected_snapshot)
        if result:
            count = result["recoverable_file_count"]
            skipped = result["excluded_git_metadata_count"]
            self.write_log(f"恢复演练通过，可恢复 {count} 个文件，排除 {skipped} 个 Git 管理条目。")
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
        if key in ("add", "save", "verify", "restore", "refresh", "edit", "settings", "worktrees"):
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
