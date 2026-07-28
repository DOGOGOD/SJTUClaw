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
ToolRegistry、审批流程和 workspace 边界。
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
| `AGENT_BACKEND` | `sjtuclaw`（默认）或 `pi` |
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

配置向导可配置主模型、联网工具、
时区、Gateway 本机/局域网访问、可选 Pi 参数、常用高级模型参数和 QQ Bot。
它不会修改默认 Agent 后端；需要 Pi 时请在具体会话中显式使用 `/pi on`。

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

原生后端只会在消息 token 达到阈值后自动压缩；也可以使用 `/compact` 手动触发。
会话空闲本身不会触发压缩。Pi 后端优先使用 Pi 自身的原生压缩。

## 安全建议

- 不要提交 `.env` 或真实 API Key。
- 为会话设置 workspace 后再执行文件写入和 Shell 操作。
- 设置 workspace 会自动启用逐回合回退；快照默认存放在 `data/workspace/rollback/`，不要手动编辑其中的 SQLite 数据库或对象文件。
- 非本机监听 Gateway 时必须设置 `GATEWAY_API_TOKEN`，并建议将
  `GATEWAY_ALLOWED_ORIGINS` 限定为实际使用的完整 `http://` 或 `https://` 来源。
- QQ Bot 凭证和允许来源应按需配置。
