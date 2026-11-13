# Work Rescue · 工作恢复助手

正在开发的跨平台终端应用。独立的文件保护内核，不绑定 AI 工具或编程语言。

当前为源码 Alpha：能在终端管理项目、创建真实加密检查点、执行恢复演练、比较差异、恢复到新目录，并启动独立的定时保护进程。尚无三系统正式发布包。实际验证状态见 [施工记录](docs/STATUS.md)。

## 启动并保护真实项目

按下文的源码安装步骤配置好依赖和备份后端后，在项目根目录启动应用。

```text
python run.py
```

也可使用 Windows 的 `start.ps1` 或 macOS/Linux 的 `start.sh`。启动器不会创建示例项目、预置口令或自动选择目录。

首次启动设置并确认自己的备份口令。选择让系统记住口令后，下次通过系统凭据解锁。进入界面后按 `a` 添加需要保护的真实项目完整路径，查看“保护范围”，点击“保存”创建首个检查点，点击“恢复演练”确认可恢复，再“开启后台”定期保护。恢复时选择具体检查点和全新目标目录。

按 `e` 可编辑项目的名称、重要产物选择和任务目标；修改范围前需停止该项目后台。按 `F2` 设置自动保存间隔；新间隔在下次启动后台时生效。应用数据和备份位置可在设置中查看。

终端内按 `Tab` 切换区域，方向键移动，`Enter` 选择；`a` 添加项目、`s` 保存、`v` 演练、`r` 恢复、`q` 退出界面。退出界面不会停止已经开启的独立后台；使用“停止后台”按钮明确停止。输入框内正常输入文字。

## 从源码安装（三系统）

需要 Python 3.12+ 和 uv。命令在项目根目录执行：

```text
uv sync --locked --extra dev
python scripts/bootstrap_restic.py
uv run work-rescue --help
```

下载脚本只在 `.tools` 存放 restic 0.19.1，按系统和架构下载并核对上游发布的 SHA-256，记录来源。摘要与归档来自同一发布源，不等同于独立签名验证。不修改全局 PATH。也可自行安装 restic 并使用 `--restic` 指定路径。

### Windows PowerShell

```powershell
$resticPath = Join-Path (Get-Location) '.tools\restic.exe'
uv run work-rescue --restic $resticPath init
uv run work-rescue --restic $resticPath
```

### macOS / Linux

```sh
uv run work-rescue --restic "$PWD/.tools/restic" init
uv run work-rescue --restic "$PWD/.tools/restic"
```

`init` 或首次打开界面时输入并确认口令。口令可选择保存到 Windows 凭据、macOS Keychain 或 Linux 系统秘密服务；没有可用服务时仍可手动输入，不退回明文文件。`--remember` 显式保存已验证口令，`--no-keyring` 本次跳过系统口令，`forget-password` 删除保存的口令且保留备份。换设备恢复仍需要自己的备份口令。登录后自动启动尚未交付。

无交互的自动化可通过 `WORK_RESCUE_PASSWORD` 注入口令；不要把真实口令提交到仓库。

应用数据默认使用系统用户数据目录；`--home` 可显式设置。仓库默认在应用数据目录的 `repository` 下，`--repo` 可指定独立位置。它们不能放在被保护项目内。

## 命令行

下面命令省略通用选项：`--home <应用数据目录> --repo <仓库> --restic <可执行文件>`，这些选项放在子命令之前。

```text
work-rescue add my-project /absolute/project --include-ignored output/ --goal "完成脚本与报表"
work-rescue scan my-project
work-rescue save my-project
work-rescue snapshots my-project
work-rescue verify <具体检查点ID>
work-rescue diff my-project <具体检查点ID>
work-rescue restore <具体检查点ID> /absolute/new-directory
work-rescue handoff <具体检查点ID>
work-rescue watch my-project --interval 300 --detach
work-rescue status my-project
work-rescue stop my-project
work-rescue doctor
work-rescue settings --interval 300
work-rescue forget-password
```

`verify` 执行完整仓库读取检查和实际恢复比对，可能比普通结构检查耗时。它与聊天能否继续是不同状态。`stop` 在正在进行的保存结束后停止后台。后台错误和最近成功时间可在界面及 `status` 查看，停止的心跳不会一直显示运行中。

## 当前保护范围

- 普通文件、未提交文件、新文件、显式选择的忽略产物；支持中文、空格、二进制和 CRLF。
- 活跃 SQLite 主文件先与 WAL/回滚日志取得稳定私有副本，再生成独立逻辑备份；哈希针对该副本，不承诺与源数据库物理字节相同。
- 逐级读取项目内 `.gitignore`，跳过被忽略的大目录，只在显式选择其产物时展开。目录排除项代表整棵子树。依赖缓存与 Git 内部目录不备份。
- 常见凭据文件默认排除并显示原因；这不是完整的秘密检测器。
- 空目录、符号链接、特殊文件暂不采集；ACL、扩展属性、NTFS 额外数据流尚不承诺保真。
- 文件稳定采集不等于全项目同一瞬间事务。尚未落盘或检查点之后的内容可能丢失。
- 原项目、Git HEAD、暂存区、原聊天不会被恢复操作覆盖；只接受全新目标目录。
- 原配置丢失后，可用仓库、口令和干净 `--home` 恢复。口令遗失则不能解密。
- 恢复先完整提取和校验，再向新目录搬入文件；搬入期间进程被强制退出可能留下部分目标，操作日志会保持未完成，不能视为成功。另选全新目录重试；不自动删除部分结果或旧备份。
- 暂存文件在正常结束时清理；强制结束留下的采集/提取暂存目录当前保留，尚未实现自动残留清理。

任务接续资料只包含用户输入的目标、保存文件清单与明确的未知状态，不把历史资料当成新测试结果。AI 工具会话适配与 MCP 尚未交付。

## 验证

```text
uv run --locked ruff check src tests scripts
uv run --locked pytest -q
```

测试使用临时人工项目和真实 restic，覆盖恢复、WAL、重复校验、原项目与 Git 不变、路径冲突、子进程强制退出、独立后台及终端交互。POSIX 执行权限测试在 Windows 明确跳过；链接测试受宿主权限限制。

`.github/workflows/test.yml` 配置了 Windows、macOS ARM/Intel、Ubuntu 22.04/24.04 与 Python 3.12/3.14 的测试矩阵。各平台的测试结果可在仓库的 Actions 页面查看。

## 许可证

本项目 MIT。restic 作为独立上游可执行程序下载，来源及许可随其发布；依赖保留各自许可证。

开发用的 `run_demo.py` 与 `scripts/demo.py` 仅用于人工数据测试，不是正常启动入口。
