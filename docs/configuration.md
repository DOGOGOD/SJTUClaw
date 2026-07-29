# 配置说明

## 快速配置

复制模板并填写模型服务信息：

```bash
cp .env.example .env
```

必填配置通常包括：

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | 模型服务 API Key；本地模型可按服务要求填写 |
| `LLM_BASE_URL` | OpenAI 兼容 API 地址 |
| `LLM_MODEL` | 模型名称 |

可直接运行 `sjtuclaw setup` 使用交互式向导。源码运行时读取项目根目录的
`.env`；安装版读取 `%USERPROFILE%\.sjtuclaw\.env`。系统环境变量优先于
`.env`，Web UI 保存的运行时设置又优先于二者。

## microsandbox 隔离运行

SJTUClaw 原生 Agent 的文件工具、Shell、附件导入和下载导出可以共享一个
session 级 microsandbox microVM。安装可选依赖：

```bash
pip install -e ".[sandbox]"
```

Windows 还需要启用 Windows Hypervisor Platform。使用本仓库旁边的
microsandbox 源码调试时，可以改为安装
`../microsandbox/sdk/python`，但源码构建仍需准备 Rust 与 microsandbox 的原生运行
依赖；发布版更适合使用包含 `msb` 与 `libkrunfw` 的官方 wheel。

推荐先构建并导入项目提供的通用依赖镜像：

```powershell
.\packaging\sandbox\Build-SandboxImage.ps1
```

构建成功后把 `SANDBOX_IMAGE` 设置为
`sjtuclaw-sandbox:py3.12-bookworm`。代码级默认值仍是
`python:3.12-bookworm`，用于没有构建自定义镜像时的基础兼容环境，但它不包含
SJTUClaw 的通用科学计算和文档处理库。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SANDBOX_MODE` | `off` | `off`、`auto` 或 fail-closed 的 `required` |
| `SANDBOX_IMAGE` | `python:3.12-bookworm` | OCI 镜像；推荐构建后设为 `sjtuclaw-sandbox:py3.12-bookworm` |
| `SANDBOX_PROJECT_VENV` | `true` | 是否启用运行 venv 并将项目依赖持久化到 `/workspace/.venv` |
| `SANDBOX_PIP_INDEX_URL` | 清华 PyPI 镜像 | 项目依赖安装源；留空时不覆盖 pip 配置 |
| `SANDBOX_CPUS` | `2` | vCPU 数 |
| `SANDBOX_MEMORY_MIB` | `2048` | 内存上限（MiB） |
| `SANDBOX_MAX_DURATION_S` | `21600` | 单个 microVM 最长生命周期 |
| `SANDBOX_IDLE_TIMEOUT_S` | `3600` | 空闲回收时间 |
| `SANDBOX_NETWORK` | `public` | `public` 允许开发联网，`none` 禁止 guest 网络 |
| `SANDBOX_SECURITY` | `restricted` | microsandbox 安全策略 |
| `SANDBOX_STAT_VIRTUALIZATION` | `auto` | `auto`、`strict`、`relaxed` 或 `off` |
| `SANDBOX_WORKSPACE_QUOTA_MIB` | `4096` | guest 对 workspace 的新增写入配额 |

行为规则：

- 默认 `off` 模式下，新 session 的 Sandbox 状态为关闭；可使用 `/sandbox on`
  单独为当前 session 开启。
- Sandbox 开启且未设置 workspace 时，第一次原生文件或 Shell 工具调用会自动
  创建 session 私有的持久卷，并挂载到 guest `/workspace`。
- 默认在每个 microVM 的 Linux rootfs 创建运行 venv，通过
  `system-site-packages` 复用镜像通用库。Shell 中的 `python`、`python3` 和
  `pip` 均使用该环境；命令结束后，项目新增包与 console scripts 会同步到
  `/workspace/.venv`，下次 microVM 启动时恢复。由于 `/workspace/.venv` 是
  持久依赖存储而不是可直接激活的 venv，不应执行
  `source /workspace/.venv/bin/activate`。若目录已存在但不是 SJTUClaw 管理的
  布局，初始化会明确报错，用户应移动、删除或重命名它。
- 推荐镜像预装 NumPy、Pandas、SciPy、Matplotlib、Seaborn、scikit-learn、
  Pillow、Requests/HTTPX、HTML 解析、OpenPyXL、PyYAML、PDF、Word 和 ReportLab
  等通用库；准确版本见
  [`packaging/sandbox/requirements-common.txt`](../packaging/sandbox/requirements-common.txt)。
  项目专属库直接用普通 `pip install` 安装，不要为了单个项目重新构建基础镜像。
- `auto` 在 Windows 上使用 `relaxed` stat virtualization 和 `mirror` 普通 rwx 位
  传播，以避开 NTFS 挂载中 guest 新文件的 ELOOP/EACCES、不可重新执行以及
  不支持 POSIX 符号链接的问题；其他宿主使用 `strict` 和 `private`。挂载始终
  保持 `nosuid`、`nodev`，且 `mirror` 不会扩大 guest 可见的宿主目录范围。
  `strict`/`relaxed` 可用于已确认支持 microsandbox 元数据 sidecar 的宿主。
- 设置 workspace 后，只把用户明确绑定的目录挂载到 guest `/workspace`；
  不会因为模型给出宿主绝对路径或 `../` 就扩大挂载范围。
- Shell 可以在 microVM 内使用 `/tmp` 等 guest 路径；结构化文件工具仍只访问
  `/workspace`，防止把宿主路径与 guest 路径混为一谈。
- 附件通过 microsandbox 文件 API 导入；下载文件先导出到 SJTUClaw 管理目录，
  再进入现有限时下载注册表。
- 文件读取和 Shell 输出都采用流式、有界处理。Tool 最多返回 64 KiB；
  单次命令 stdout 与 stderr 合计超过 8 MiB 时进程会被终止并明确报错。
  非 UTF-8 输出会使用替换字符展示。
- 覆盖、编辑和附件导入使用同目录临时文件后 rename 提交，失败时不会把目标文件
  留在部分写入状态。
- `required` 模式下，SDK 缺失、WHP 未启用、镜像拉取失败或 microVM 启动失败都会
  明确报错，不会悄悄改回宿主执行。该模式也禁止 `UNLIMITED`，并拒绝切换到目前
  尚未纳入 microVM 的 Pi / Claude Code 后端。
- `auto` 模式会先运行一次本地能力检查，只有 SDK、原生运行文件与 hypervisor
  都就绪时才启用；显式打开 `UNLIMITED` 仍表示用户选择
  原有宿主级不受限行为。生产或处理不可信代码时应使用 `required`。

可在任意对话入口使用：

```text
/sandbox          # 显示相关命令的简要说明
/sandbox on       # 只开启当前 session
/sandbox off      # 只关闭当前 session
/sandbox status   # 查看模式、镜像、运行状态和 workspace 类型
```

不同 session 的开关互不影响；Web UI 会在 sandbox 当前生效的 session 上显示
Sandbox 图标。关闭时会停止当前 microVM，再次开启后由下次工具调用按需重建。
`required` 模式不允许关闭。显式 on/off 状态写入 Session metadata，CLI、
Gateway 或桌面应用重启后继续生效；仅从未显式设置过的 session 才使用
`SANDBOX_MODE` 默认值。若持久状态为开启但重启后 SDK、虚拟化或运行文件不可用，
文件和 Shell 工具会 fail-closed，不会回退宿主执行。改变 workspace 绑定也会
自动重建 microVM，确保旧挂载不会继续生效。停止或状态保存失败时命令会返回错误，
不会误报成功。
修改上述环境变量后需要重启 CLI 或 Gateway。
任务运行期间使用 `/stop` 时，Gateway 还会停止该 session 的 microVM，以中断
正在等待的 sandbox 命令；私有命名卷不会因此丢失。

安装后可执行真实环境冒烟测试：

```bash
python tests/sandbox_smoke.py --image alpine:3.21 --no-project-venv
python tests/sandbox_smoke.py --image alpine:3.21 --no-project-venv --workspace C:\path\to\test-workspace
python tests/sandbox_smoke.py --image sjtuclaw-sandbox:py3.12-bookworm --verify-common-libs
```

前两条使用非 Python 镜像分别验证私有命名卷和显式宿主目录挂载；第三条同时验证
项目依赖同步层和基础镜像通用库。脚本会实际启动 microVM，离线安装测试 wheel，
重启 microVM 后验证包和 console script，并完成 Shell、文件读写、
导出和清理；镜像尚未缓存时需要访问 OCI registry。自定义镜像的构建与
`msb load` 方法见 [Sandbox 基础镜像](../packaging/sandbox/README.md)。生产配置
建议将 `SANDBOX_IMAGE` 固定到团队审核过的镜像 digest。

依赖隔离由 workspace 决定：

- 未绑定宿主 workspace：每个 session 使用自己的 microsandbox 命名卷，
  `/workspace/.venv` 也彼此独立；删除 session 时会请求删除该卷。
- 已绑定宿主 workspace：`.venv` 就在绑定目录中；删除 session 不删除该目录，
  绑定同一个物理目录的其他 session 会复用其中的项目依赖。
- `/sandbox off` 只停止 microVM，不删除 workspace 或 `.venv`。
- 新建和 fork session 不继承来源 session 的 AUTO、Sandbox 或 UNLIMITED 状态；
  删除 session 时 AUTO/Sandbox metadata 随 Session 一并删除。

AUTO 同样持久化在 Session metadata 中，重启后保持；UNLIMITED 仍只保存在内存中，
进程重启后关闭。Rollback 只恢复对话和 workspace，不改变回退操作发生时的
AUTO/Sandbox 状态，避免历史检查点意外恢复旧权限。

## Pi Agent 后端

设置 `AGENT_BACKEND=pi` 后，新建会话默认由 Pi coding agent 的官方 RPC 模式执行；
标题生成、反思等辅助服务仍复用现有 LLM 配置。Pi 的模型提供商、工具循环、
Skills、Extensions、自动压缩、重试和持久会话均保留，WebUI、QQ 与桌宠接口不变。
也可以在 CLI、WebUI 或 QQ 对话中输入 `/pi`，查看当前 session 的 Agent 后端和
可用指令；只有显式输入 `/pi on` 时，系统才会检查 Pi 运行环境并为当前 session
启用 Pi。`/pi off` 仅将当前 session 切回 SJTUClaw 原生后端。每个 session 的
选择独立持久化。

SJTUClaw 不替换 Pi 的默认 system prompt。Pi 会根据实际启用的工具自动生成
`Available tools`、每个工具的 `promptSnippet` 与 `promptGuidelines`；SJTUClaw
只追加身份、人格、长期记忆和运行环境。长期记忆、Web、Cron、下载等宿主工具
通过 Extension 桥接进入同一份 Pi 工具清单与 schema，并继续使用 SJTUClaw
ToolRegistry 和审批流程。绑定 workspace 时，该目录仅作为 Pi 的启动目录；
SJTUClaw 不对 Pi 原生 `read`/`bash`/`edit`/`write` 工具施加 workspace 越界限制，
访问范围由 Pi 自身规则和操作系统权限决定。宿主桥接工具仍遵守各自工具契约。
Pi 后端下的 `/compact` 会优先调用 Pi RPC 的原生压缩命令；如果 Pi
会话过短或原生压缩失败，但统一会话历史仍可压缩，则回退到 SJTUClaw
会话压缩。回退路径需要已配置可用的辅助 LLM。

启动前先构建相邻的 `pi` 仓库，或把 `pi` 安装到系统命令路径。SJTUClaw 按
`PI_COMMAND`、`PI_CLI_PATH`、相邻 Pi 构建产物、系统 `pi` / `pi.cmd` 的顺序查找。
源码仓库布局为 `SJTUClaw/SJTUClaw` 与 `SJTUClaw/pi` 时可自动发现。Windows
安装版不会内置完整 Pi/Node 运行时，需要另外安装系统 `pi`，或显式设置
`PI_COMMAND` / `PI_CLI_PATH`（以及必要时的 `PI_NODE_PATH`）。

Pi 是可选外部依赖，不是 SJTUClaw 仓库的一部分。发布或上传 SJTUClaw 到 GitHub
时，应提交 `claw/pi/` 下的桥接代码、前后端接入代码、测试、文档和已跟踪的 Web
构建产物；不应把同级的完整 `pi` SDK 仓库直接复制进本仓库。若需要固定 Pi 版本，
建议在文档中记录 Pi 的来源、版本或 commit，或按项目策略使用 Git submodule。

如果相邻的 `pi` 目录被删除，但 `PI_COMMAND`、`PI_CLI_PATH` 或系统 `PATH` 中仍有
可执行 Pi，`/pi on` 仍可启用当前 session 的 Pi 后端。若所有入口都不可用，
`/pi on` 会返回 Pi 运行环境不可用的错误，且不会把当前 session 切换到不可运行的
Pi 状态。

| 变量 | 说明 |
| --- | --- |
| `AGENT_BACKEND` | `sjtuclaw`（默认）、`pi` 或 `claude` |
| `PI_COMMAND` | 完整 Pi 启动命令 |
| `PI_CLI_PATH` / `PI_NODE_PATH` | Pi `cli.js` 与 Node.js 路径 |
| `PI_SHELL_PATH` | Windows 下可选的 Bash 路径；优先推荐 Git Bash/MSYS Bash |
| `PI_REPO_DIR` | Pi 源码路径；默认是 SJTUClaw 相邻的 `pi` |
| `PI_PROVIDER` / `PI_MODEL` | 可选 provider 与 model；留空使用 Pi 设置 |
| `PI_THINKING` | `off` 到 `max` 的 Pi reasoning level |
| `PI_REASONING` | 将现有 `LLM_*` 映射给 Pi 时是否声明模型支持 reasoning，默认 `false` |
| `PI_CWD` | Pi 工具工作目录 |
| `PI_AGENT_DIR` / `PI_SESSION_DIR` | Pi 配置与持久会话目录 |
| `PI_TURN_TIMEOUT_S` | 单轮最长秒数，默认 1800 |
| `PI_TRUST_TOOLS` | 跳过写入审批；默认 `false`，仅可信环境使用 |

Windows 下 SJTUClaw 会优先探测可正常运行的 Git Bash/MSYS Bash，并避开旧版 WSL
`bash.exe` 启动器；只能使用 WSL 时会启用 UTF-8 诊断输出，防止 WebUI 工具结果乱码。
Pi 的 Bash 命令应优先使用当前工作目录和相对路径，不要直接传入带反斜杠的
`C:\...` 路径。

Pi 本身不提供宿主权限沙箱。SJTUClaw 默认加载一个薄 Extension，把 `bash`、
`edit`、`write` 转交给现有审批通道；没有审批通道时安全拒绝。

如果没有设置 `PI_PROVIDER` 和 `PI_MODEL`，但已有完整 `LLM_API_KEY`、
`LLM_BASE_URL`、`LLM_MODEL`，SJTUClaw 会通过进程环境把它们注册成 Pi 的
`sjtuclaw` OpenAI-compatible provider。密钥不会写入 Pi 配置文件或命令行。
显式设置 `PI_PROVIDER`/`PI_MODEL` 时则完全使用 Pi 自身的 auth 与 models 配置。

## Claude Code 后端

设置 `AGENT_BACKEND=claude` 后，新建会话默认由本机 Claude Code 执行。也可以在
CLI、WebUI 或 QQ 对话中使用 `/claude on` 为当前 session 启用，使用
`/claude off` 切回 SJTUClaw 原生后端。每个 session 的选择和 Claude 会话映射
独立持久化；从其他后端切回 Claude Code、变更 workspace、清空或回退会话时，
系统会创建新的 Claude 分支并自动交接 SJTUClaw 中的有效历史，避免恢复到过期状态。

SJTUClaw 不保存或转换 Claude Code 凭据，而是沿用本机已有的 Claude Code 登录、
provider、模型、`CLAUDE.md`、Skills、MCP、hooks 和权限规则。工具调用通过官方
`-p --output-format stream-json` 事件流显示在统一会话中，`/stop` 会终止当前
Claude Code 进程。SJTUClaw 使用 `--append-system-prompt-file` 追加集成上下文，
不会替换或删除 Claude Code 的原生 prompt。Claude Code 自行管理和压缩原生会话
上下文。

自动检索顺序如下：

1. `CLAUDE_CODE_COMMAND` 或 `CLAUDE_CODE_PATH`；
2. 系统 `PATH` 中的 `claude` / `claude.exe` / `claude.cmd`；
3. 官方原生安装目录 `~/.local/bin/claude`（Windows 为
   `%USERPROFILE%\.local\bin\claude.exe`）；
4. 旧版 `~/.claude/local` 和常见 npm 全局安装目录。

如果 `/claude on` 未找到可执行文件，会保持当前 session 原后端不变并返回安装提示。
首次使用前请先按照
[Claude Code 官方安装文档](https://code.claude.com/docs/en/installation)
安装并登录。

| 变量 | 说明 |
| --- | --- |
| `CLAUDE_CODE_PATH` | Claude Code 可执行文件路径；通常无需设置 |
| `CLAUDE_CODE_COMMAND` | 完整启动命令；优先级高于自动检索 |
| `CLAUDE_CODE_MODEL` | 可选模型或别名；留空沿用 Claude Code 默认值 |
| `CLAUDE_CODE_PERMISSION_MODE` | Claude Code 原生权限模式，默认 `default` |
| `CLAUDE_CODE_CWD` | 默认工作目录；session 绑定 workspace 时以后者为准 |
| `CLAUDE_CODE_TURN_TIMEOUT_S` | 单轮最长秒数，默认 1800 |
| `CLAUDE_CODE_TRUST_TOOLS` | 跳过 SJTUClaw 与 Claude Code 审批；默认 `false`，仅完全可信环境使用 |

默认使用 Claude Code 的 `default` 权限模式。SJTUClaw 按“是否改变状态”判断危险
操作：文件写入/编辑/删除、有副作用的 Shell 命令，以及创建、更新、发送、部署等
MCP 调用会在执行前进入统一审批；WebSearch、WebFetch、读取、查询和搜索不会弹出
SJTUClaw 审批。Claude Code 自身的权限配置和 Hooks 仍然保留。即使选择
`acceptEdits`，上述状态变更操作仍需通过 SJTUClaw 审批。只有完全信任当前环境时
才应启用 `CLAUDE_CODE_TRUST_TOOLS=true`。

与 Pi 相同，Claude Code 会通过每回合临时创建的本地 MCP 服务获得 SJTUClaw 独有
工具，例如 `recall`、`remember`、`cron`、`current_time`、`web_search` 和
`web_fetch`。与 Claude Code 原生能力等价的文件、Shell 和 Skill 工具不会重复暴露；
启动时不使用 `--strict-mcp-config`，因此用户原有的 Claude Code MCP 配置会继续保留。

绑定 workspace 时，该目录仅作为 Claude Code 的启动目录；SJTUClaw 不对 Claude
Code 原生文件或命令工具施加 workspace 越界限制。实际可访问范围仍由 Claude Code
权限配置、操作系统权限和可能启用的外部沙箱决定。

配置向导只配置主模型、联网工具、时区、Gateway 本机/局域网访问、常用高级模型
参数和 QQ Bot；其中不包含 Agent 后端或 Pi 配置。外部后端可在具体会话中使用
`/pi on` 或 `/claude on`，也可在 Web 设置页配置新会话默认后端。

## 时区

时间相关功能默认自动识别系统时区。无法识别时使用上海时区 `Asia/Shanghai`，也可以通过环境变量显式覆盖：

```env
CLAW_TIMEZONE=Asia/Shanghai
```

建议使用 IANA 时区名称，例如 `Asia/Shanghai`、`America/New_York` 或 `Europe/London`。

## 常用配置

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `GATEWAY_HOST` / `GATEWAY_PORT` | Gateway 监听地址和端口 | `127.0.0.1` / `8000` |
| `GATEWAY_API_TOKEN` | Gateway API 认证令牌 | 空 |
| `GATEWAY_ALLOWED_ORIGINS` | 非本机访问允许的浏览器来源，逗号分隔 | 空 |
| `GATEWAY_OPEN_BROWSER` | 启动 Gateway 后自动打开浏览器 | `false` |
| `CLAW_MAX_AGENT_ITERATIONS` | 单轮 Agent 最大迭代次数 | `15` |
| `CLAW_MAX_TOOL_CALLS_PER_TURN` | 单轮工具调用上限 | `20` |
| `COMPACT_MAX_MESSAGE_TOKENS` | 原生后端自动压缩的消息 token 阈值 | `2000` |
| `COMPACT_KEEP_RECENT_TOKENS` | 压缩时保留的最近消息 token 预算 | `1000` |
| `HEARTBEAT_INTERVAL_S` | Heartbeat 检查间隔 | `1800` |
| `SJTUCLAW_USER_DIR` | 安装版用户根目录覆盖值 | `%USERPROFILE%\.sjtuclaw` |
| `SJTUCLAW_DATA_DIR` | 运行数据目录覆盖值 | 源码版 `data/`；安装版用户根目录下的 `data/` |

所有可用变量及注释见 [`.env.example`](../.env.example)。

## Web UI 文件下载

默认模式下，`create_download` 只接受当前 session workspace 中已经存在的文件；
UNLIMITED 模式会按其既有规则放宽路径边界。工具成功后返回
`displayMarkdown`，Gateway 会确保最终回复中存在唯一的 `/downloads/<downloadId>`
入口：普通文件在 Web UI 中显示为下载按钮，PNG、JPEG、GIF、WebP、BMP 和 AVIF
还可以安全地内联预览。点击图片的下载按钮时，前端会显式请求附件响应，避免浏览器
只打开预览页。

下载入口默认有效一小时，最多保留 1000 条。注册表位于
`data/downloads/registry.json`，因此仍在有效期内且源文件仍存在的链接可跨 Gateway
重启继续使用。注册表只保存下载 ID、源文件绝对路径和创建时间，不复制文件内容；
删除或移动源文件会使入口失效。`/downloads/{id}` 只能访问已登记且格式合法的
`dl_<12位十六进制>` ID，不能作为任意文件读取接口。

原生后端只会在消息 token 达到阈值后自动压缩；也可以使用 `/compact` 手动触发。
会话空闲本身不会触发压缩。自动压缩完成会在 CLI 和 WebUI 显示完成通知，
`/compact` 会返回带摘要预览的压缩简报。压缩仅处理完整旧轮次；任务运行期间
不会执行手动压缩，也不会停止或截断任务。Pi 后端优先使用 Pi 自身的原生压缩；
Claude Code 后端由 Claude Code 自动管理上下文。

## 安全建议

- 不要提交 `.env` 或真实 API Key。
- 为会话设置 workspace 后再执行文件写入和 Shell 操作。
- 设置 workspace 会自动启用逐回合回退；快照默认存放在 `data/workspace/rollback/`，不要手动编辑其中的 SQLite 数据库或对象文件。
- 非本机监听 Gateway 时必须设置 `GATEWAY_API_TOKEN`，并建议将
  `GATEWAY_ALLOWED_ORIGINS` 限定为实际使用的完整 `http://` 或 `https://` 来源。
- QQ Bot 凭证和允许来源应按需配置。
