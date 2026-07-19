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
