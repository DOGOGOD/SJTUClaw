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

## Pi Agent 后端

设置 `AGENT_BACKEND=pi` 后，主对话由 Pi coding agent 的官方 RPC 模式执行；
标题生成、反思等辅助服务仍复用现有 LLM 配置。Pi 的模型提供商、工具循环、
Skills、Extensions、自动压缩、重试和持久会话均保留，WebUI、QQ 与桌宠接口不变。

启动前先构建相邻的 `pi` 仓库，或把 `pi` 安装到系统命令路径。SJTUClaw 按
`PI_COMMAND`、`PI_CLI_PATH`、相邻 Pi 构建产物、系统 `pi` 的顺序查找。

| 变量 | 说明 |
| --- | --- |
| `AGENT_BACKEND` | `sjtuclaw`（默认）或 `pi` |
| `PI_COMMAND` | 完整 Pi 启动命令 |
| `PI_CLI_PATH` / `PI_NODE_PATH` | Pi `cli.js` 与 Node.js 路径 |
| `PI_REPO_DIR` | Pi 源码路径；默认是 SJTUClaw 相邻的 `pi` |
| `PI_PROVIDER` / `PI_MODEL` | 可选 provider 与 model；留空使用 Pi 设置 |
| `PI_THINKING` | `off` 到 `max` 的 Pi reasoning level |
| `PI_CWD` | Pi 工具工作目录 |
| `PI_AGENT_DIR` / `PI_SESSION_DIR` | Pi 配置与持久会话目录 |
| `PI_TURN_TIMEOUT_S` | 单轮最长秒数，默认 1800 |
| `PI_TRUST_TOOLS` | 跳过写入审批；默认 `false`，仅可信环境使用 |

Pi 本身不提供宿主权限沙箱。SJTUClaw 默认加载一个薄 Extension，把 `bash`、
`edit`、`write` 转交给现有审批通道；没有审批通道时安全拒绝。

也可以运行 `sjtuclaw setup` 使用交互式配置向导。

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
| `CLAW_MAX_AGENT_ITERATIONS` | 单轮 Agent 最大迭代次数 | `15` |
| `CLAW_MAX_TOOL_CALLS_PER_TURN` | 单轮工具调用上限 | `20` |
| `COMPACT_IDLE_TTL_MINUTES` | 空闲会话压缩阈值 | `60` |
| `HEARTBEAT_INTERVAL_S` | Heartbeat 检查间隔 | `1800` |

所有可用变量及注释见 [`.env.example`](../.env.example)。

## 安全建议

- 不要提交 `.env` 或真实 API Key。
- 为会话设置 workspace 后再执行文件写入和 Shell 操作。
- 设置 workspace 会自动启用逐回合回退；快照默认存放在 `data/workspace/rollback/`，不要手动编辑其中的 SQLite 数据库或对象文件。
- 非本机监听 Gateway 时设置 `GATEWAY_API_TOKEN`。
- QQ Bot 凭证和允许来源应按需配置。
