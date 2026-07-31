# 配置说明

SJTUClaw 可以通过 Web UI 设置、系统环境变量或 `.env` 配置。建议普通用户使用 Web UI，开发者使用项目根目录的 `.env`。

## 生效顺序

对支持 Web UI 修改的配置，优先级为：

1. `data/settings/runtime_settings.json`
2. 已存在的系统环境变量
3. `.env`
4. 代码默认值

`LLM_API_KEY`、`COMPACT_LLM_API_KEY` 和 `QQ_CLIENT_SECRET` 在运行设置文件中使用 Fernet 加密。密钥单独保存在 `data/settings/runtime_settings.key`。

Gateway 地址、部分后台服务和底层重试参数直接读取环境变量；修改后应重启当前进程。

## 最小配置

原生 Agent 需要：

```env
LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o
```

运行配置向导：

```powershell
sjtuclaw setup
```

Gateway 即使没有原生 LLM 凭证也能启动设置界面；已配置的 Pi 或 Claude Code Session 仍可使用。

## Agent 后端

```env
AGENT_BACKEND=sjtuclaw
```

允许值：`sjtuclaw`、`pi`、`claude`。该值决定新 Session 的默认后端；现有 Session 可以用 `/pi on`、`/claude on` 或设置界面单独切换。

### Pi

最常用配置：

```env
AGENT_BACKEND=pi
PI_COMMAND=pi
PI_PROVIDER=openai
PI_MODEL=gpt-5.2-codex
PI_THINKING=high
PI_TURN_TIMEOUT_S=1800
PI_TRUST_TOOLS=false
```

命令解析顺序：

1. `PI_COMMAND`
2. `PI_CLI_PATH` 配合 `PI_NODE_PATH`
3. 项目或系统中可发现的 Pi CLI

其他可选项：

| 配置 | 作用 |
| --- | --- |
| `PI_REPO_DIR` | Pi 仓库位置，用于发现 CLI 和 Shell |
| `PI_SHELL_PATH` | Pi 在 Windows 使用的 Shell |
| `PI_CWD` | Pi 默认工作目录 |
| `PI_AGENT_DIR` | Pi Agent 配置目录 |
| `PI_SESSION_DIR` | Pi 原生会话目录，默认 `data/pi/sessions` |
| `PI_REASONING` | OpenAI Compatible Provider 是否启用 reasoning |

`PI_TRUST_TOOLS=true` 会跳过 SJTUClaw 对 Pi 原生工具的审批，只应在完全可信环境使用。

### Claude Code

```env
AGENT_BACKEND=claude
CLAUDE_CODE_MODEL=sonnet
CLAUDE_CODE_PERMISSION_MODE=default
CLAUDE_CODE_TURN_TIMEOUT_S=1800
CLAUDE_CODE_TRUST_TOOLS=false
```

SJTUClaw 会依次检查：

1. `CLAUDE_CODE_COMMAND`
2. `CLAUDE_CODE_PATH`
3. `PATH`
4. Windows 用户目录中的官方安装位置和常见 npm 位置

可用项：

| 配置 | 作用 |
| --- | --- |
| `CLAUDE_CODE_CWD` | Claude Code 默认工作目录 |
| `CLAUDE_CODE_MODEL` | 传给 Claude Code 的模型 |
| `CLAUDE_CODE_PERMISSION_MODE` | Claude Code 原生权限模式 |
| `CLAUDE_CODE_TURN_TIMEOUT_S` | 单轮超时，默认 1800 秒 |
| `CLAUDE_CODE_TRUST_TOOLS` | 跳过 SJTUClaw 审批桥接 |

Claude Code 保留本机登录、Skills、MCP 和原生会话。SJTUClaw 通过 `stream-json` 接收事件，并用本地审批桥接处理危险操作。当前 Session 开启 `AUTO` 后，Pi 与 Claude Code 的原生写入、编辑和危险命令也会自动批准；`UNLIMITED` 同时开启时仍强制逐次审批。

## 上下文与 Agent Loop

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `LLM_CONTEXT_WINDOW` | `32000` | 模型上下文窗口 |
| `LLM_CONTEXT_USAGE_RATIO` | `0.80` | 允许使用的窗口比例 |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` | 为回复保留的预算 |
| `LLM_CONSOLIDATION_RATIO` | `0.50` | 多轮压缩后的目标比例 |
| `COMPACT_MAX_MESSAGE_TOKENS` | `2000` | 自动压缩触发阈值 |
| `COMPACT_KEEP_RECENT_TOKENS` | `1000` | 压缩时保留的近期 Token |
| `COMPACT_KEEP_RECENT_MESSAGES_MIN` | `4` | 至少保留的近期消息数 |
| `HISTORY_MAX_ENTRIES` | `2000` | 会话日志保留上限 |
| `CLAW_MAX_TOOL_CALLS_PER_TURN` | `20` | 单次模型响应最多工具调用数 |
| `CLAW_MAX_AGENT_ITERATIONS` | `15` | 单个用户回合最多 Agent 迭代数 |

可为压缩单独配置模型：

```env
COMPACT_LLM_API_KEY=
COMPACT_LLM_BASE_URL=
COMPACT_LLM_MODEL=
```

留空时使用主模型。Pi 使用自身压缩能力；Claude Code 管理自身上下文。

LLM 请求可靠性：

| 配置 | 默认值 |
| --- | ---: |
| `LLM_MAX_RETRIES` | `2` |
| `LLM_RETRY_BASE_DELAY` | `1.0` 秒 |
| `LLM_REQUEST_TIMEOUT` | `120` 秒 |

## Sandbox

```env
SANDBOX_MODE=off
SANDBOX_IMAGE=sjtuclaw-sandbox:py3.12-bookworm
SANDBOX_PROJECT_VENV=true
```

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `SANDBOX_MODE` | `off` | `off`、`auto` 或 `required` |
| `SANDBOX_IMAGE` | `python:3.12-bookworm` | microVM 镜像；示例文件推荐项目镜像 |
| `SANDBOX_CPUS` | `2` | vCPU 数 |
| `SANDBOX_MEMORY_MIB` | `2048` | 内存上限 |
| `SANDBOX_MAX_DURATION_S` | `21600` | 最长存活时间 |
| `SANDBOX_IDLE_TIMEOUT_S` | `3600` | 空闲回收时间 |
| `SANDBOX_NETWORK` | `public` | `public` 或 `none` |
| `SANDBOX_SECURITY` | `restricted` | `restricted` 或 `default` |
| `SANDBOX_STAT_VIRTUALIZATION` | `auto` | `auto`、`strict`、`relaxed`、`off` |
| `SANDBOX_WORKSPACE_QUOTA_MIB` | `4096` | 私有卷或新增写入配额 |
| `SANDBOX_PROJECT_VENV` | `true` | 是否维护 `/workspace/.venv` |
| `SANDBOX_PIP_INDEX_URL` | 清华 PyPI 镜像 | 项目依赖安装源 |

`required` 采用 fail-closed：Sandbox 不可用时拒绝执行，不回退宿主；同时禁用 UNLIMITED，并拒绝 Pi / Claude Code 后端。

## Workspace Rollback

设置 Workspace 本身不会开启 Rollback。用户必须执行 `/rollback on`
显式开启；此后该 Session 才会在每个用户回合前创建检查点。

Rollback 使用持久增量哈希缓存：未变化文件不会在每个回合重复读取，
新文件或已变化文件采用单次流式读取，同时完成哈希和快照对象写入。

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `ROLLBACK_MAX_FILES` | `100000` | 单次扫描最多检查的文件与链接数 |
| `ROLLBACK_MAX_FILE_BYTES` | `134217728` | 单个新快照文件上限（128 MiB） |
| `ROLLBACK_MAX_SNAPSHOT_BYTES` | `268435456` | 单次最多新读取的数据量（256 MiB） |
| `ROLLBACK_SCAN_TIMEOUT_S` | `5` | 单次扫描时间上限（秒） |
| `ROLLBACK_SCAN_WORKERS` | `4` | 新内容并行哈希与对象写入线程数（1–16） |

达到任一预算时，检查点会标记为“部分快照”，并保留具体警告。
恢复部分快照时只恢复明确记录的文件；扫描覆盖不完整时不会推断并删除
额外路径。后续检查点会复用已缓存内容，因此在总数据预算触发后可以
逐回合补齐尚未缓存的普通文件。未缓存文件会优先按体积从小到大处理，
并在受控线程池中并行捕获，以便在固定时间预算内覆盖更多路径。
回退操作会直接复用“回退前安全点”的扫描结果，避免在应用文件前再次
遍历整个 Workspace。若该安全点是部分快照，只修改其中已明确捕获、
可由补偿或 `/rollback undo` 恢复的路径；未覆盖路径保持原状。

> **注意：rollback功能仍不完善，workspace中文件过多时不建议使用。**

## Gateway 与 Web UI

```env
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8000
GATEWAY_OPEN_BROWSER=false
```

监听非回环地址时必须设置：

```env
GATEWAY_API_TOKEN=至少32字符的随机令牌
GATEWAY_ALLOWED_ORIGINS=http://192.168.1.10:8000
```

远程请求通过 `Authorization: Bearer <token>` 或 `X-SJTUClaw-Token` 认证。浏览器请求的 Origin 必须在允许列表内。

## 联网工具

```env
WEB_TOOL_ENABLED=true
WEB_TIMEOUT_SECONDS=15
WEB_MAX_RESPONSE_BYTES=2097152
WEB_MAX_RETRIES=2
WEB_TRUST_ENV_PROXY=false
TAVILY_API_KEY=
```

配置 Tavily 后优先使用 Tavily；否则使用 DuckDuckGo，并在失败时回退 Bing。`web_fetch` 会限制协议、重定向、响应大小和私有地址访问。

## Heartbeat、Reflection 与时区

```env
CLAW_TIMEZONE=Asia/Shanghai
HEARTBEAT_ENABLED=true
HEARTBEAT_INTERVAL_S=1800
HEARTBEAT_KEEP_RECENT=8
```

`CLAW_TIMEZONE` 使用 IANA 时区名。留空时自动识别系统时区，失败后回退 `Asia/Shanghai`。

Reflection 的启用状态和执行时间不使用环境变量，而保存在 `data/memory/reflection_config.json`，可通过 `/reflect` 或 Web UI 修改。

## QQ Bot

```env
QQ_ENABLED=false
QQ_APP_ID=
QQ_CLIENT_SECRET=
QQ_ALLOW_FROM=
QQ_MSG_FORMAT=markdown
```

- `QQ_ALLOW_FROM` 使用英文逗号分隔 OpenID。
- 留空表示拒绝所有用户；`*` 表示允许所有用户，不建议用于生产环境。
- `QQ_ACK_MESSAGE` 可以设置收到请求后的确认文本。
- `QQ_MEDIA_DIR` 可以覆盖附件保存目录。
- 代理按 `WSS_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 的顺序选择。

扫码连接：

```powershell
python -m claw.channels.qq_onboard
```

## 路径覆盖

| 配置 | 作用 |
| --- | --- |
| `SJTUCLAW_USER_DIR` | 覆盖安装版用户根目录 |
| `SJTUCLAW_DATA_DIR` | 覆盖运行数据目录 |

源码版默认使用项目根目录和项目内 `data/`；安装版默认使用 `%USERPROFILE%\.sjtuclaw`。

## 安全建议

- 不提交 `.env`、`runtime_settings.key` 或任何真实密钥。
- 对外监听 Gateway 时同时启用 Token、精确 Origin 和主机防火墙。
- 不在不可信环境开启 `PI_TRUST_TOOLS` 或 `CLAUDE_CODE_TRUST_TOOLS`。
- `AUTO` 不解除路径边界；`UNLIMITED` 不取消审批。
- 需要强隔离时使用 `SANDBOX_MODE=required`，并保持 `SANDBOX_SECURITY=restricted`。
