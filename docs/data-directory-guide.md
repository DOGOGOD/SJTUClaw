# 运行数据目录

SJTUClaw 把可写运行状态集中放在 `data/`。源码版默认使用项目内 `data/`；安装版默认使用 `%USERPROFILE%\.sjtuclaw\data`。

可用环境变量覆盖：

```env
SJTUCLAW_USER_DIR=D:\SJTUClaw
SJTUCLAW_DATA_DIR=D:\SJTUClaw\data
```

## 目录总览

实际目录按启用功能按需创建。

```text
data/
├── sessions/                 Session JSONL、附件
├── memory/                   长期记忆、Reflection 配置
├── cron/                     定时任务和运行输出
├── workspace/                Workspace 绑定与回退对象
├── settings/                 Web UI 运行设置和加密密钥
├── downloads/                文件交付注册表
├── pet/                      桌宠设置和互动台词
├── pets/                     用户导入的宠物包内容
├── pi/sessions/              Pi 原生会话
├── claude/runtime/           Claude Code 审批与桥接临时文件
├── runtime/microsandbox/     microsandbox 运行时缓存
└── sandbox/exports/          从 microVM 导出的交付文件
```

安装版还会按需把可编辑的内置资源复制到：

```text
data/prompts/
data/skills/
```

## `sessions/`

每个 SJTUClaw Session 使用一个 JSONL 文件。文件名由 Session ID 编码，内容包括：

- 第一行：Session 元数据、标题、摘要、压缩边界和运行偏好。
- 后续行：用户、助手和工具消息。
- 工具消息保留 `tool_calls`、`tool_call_id` 和工具名。
- 图片路径、回退检查点、延迟等非文本信息随消息保存。

旧版 `session.json` 会自动迁移。写入使用文件锁、临时文件和原子替换，损坏的单行不会使整个会话不可读。

附件保存在：

```text
data/sessions/<session-id>/attachments/
```

每个 Session 独立管理附件，Gateway 不允许跨 Session 直接读取。

Pi 的原生会话存放在 `data/pi/sessions/`；Claude Code 使用自身会话存储，`data/claude/runtime/` 只保存 SJTUClaw 为命令、MCP 宿主工具和审批桥接生成的短期文件。

## `memory/`

长期记忆按类别保存为 Markdown：

```text
data/memory/<category>/mem_<number>.md
```

每个文件包含 YAML Front Matter 和正文。Memory Store 启动时加载索引，`recall` 在内存中检索，增删改后再落盘。

常见类别包括事实、偏好、项目、决策和技能经验。旧版 `memory.json` 会自动迁移。

每日 Reflection 配置和最近运行记录位于：

```text
data/memory/reflection_config.json
```

## `cron/`

```text
data/cron/
├── jobs.json
└── runs/<job-id>/<timestamp>.md
```

`jobs.json` 保存一次性、固定间隔和 Cron 表达式任务，以及启用状态、下次执行时间、来源 Session、投递渠道和运行统计。

每次 Agent 定时任务的输出写入 `runs/`，可作为审计记录或后续任务依赖。每个任务只保留有限数量的最新输出。

Heartbeat 是受保护的系统任务，也由同一调度服务管理。

## `workspace/`

```text
data/workspace/
├── bindings.json
└── rollback/
    ├── state.db
    └── objects/
```

- `bindings.json`：Session 到宿主 Workspace 的持久映射。
- `state.db`：回退检查点、分支、文件清单、增量文件哈希缓存，以及
  `/rollback on` / `/rollback off` 的 Session 级持久开关。
- `objects/`：按内容寻址保存的文件对象。

回退不会直接删除原始 Session 历史，而是恢复工作区快照并建立新的可见对话分支。`state.db-wal` 和 `state.db-shm` 是 SQLite 正常运行文件，不应单独删除。

> **注意：rollback功能仍不完善，workspace中文件过多时不建议使用。**

## `settings/`

```text
data/settings/
├── runtime_settings.json
└── runtime_settings.key
```

Web UI 修改的模型、Agent、QQ 和部分运行参数保存在这里。敏感字段使用 `runtime_settings.key` 加密。

这两个文件需要配套备份。只保留 JSON 而丢失 Key，会导致其中的密文无法解密。

## `downloads/` 与 `sandbox/exports/`

`data/downloads/registry.json` 保存 `create_download` 注册的 ID 与源文件路径，最多保留 1000 条。注册记录可以跨 Gateway 重启恢复，但源文件必须继续存在。

microVM 内文件无法直接由宿主 Web UI 提供下载，因此会先复制到：

```text
data/sandbox/exports/
```

清理导出文件会使对应下载链接失效。

## `pet/` 与 `pets/`

```text
data/pet/
├── settings.json
└── replies/<pet-id>.json

data/pets/<pet-id>/
├── pet.json
└── spritesheet.webp
```

- `settings.json`：启用状态、当前宠物、自启动和窗口位置。
- `replies/`：按宠物保存 LLM 生成或备用的点击台词。
- `pets/`：用户导入并通过校验的自定义宠物资源。

内置宠物位于应用资源目录，不写入 `data/pets/`。

## Sandbox 数据边界

`data/runtime/microsandbox/` 仅用于 SJTUClaw 侧的运行时缓存和兼容文件。microVM 镜像与私有 Volume 通常由 microsandbox 保存在其自己的 `MSB_HOME`（常见为 `%USERPROFILE%\.microsandbox`），不属于 SJTUClaw 的 `data/`。

因此，备份 `data/` 不等于备份 Sandbox 私有 Workspace；两者需要分别处理。

## 清理与备份

| 目标 | 删除后的影响 |
| --- | --- |
| `sessions/` | 会话、附件和外部后端映射丢失 |
| `memory/` | 长期记忆和 Reflection 状态丢失 |
| `cron/` | 定时任务与运行记录丢失 |
| `workspace/` | Workspace 绑定和回退历史丢失 |
| `settings/` | Web UI 配置丢失，需重新填写密钥 |
| `downloads/`、`sandbox/exports/` | 已交付文件链接失效 |
| `pet/`、`pets/` | 桌宠设置、台词和自定义宠物丢失 |
| `pi/sessions/` | Pi 原生会话丢失 |

停止 SJTUClaw 后再备份整个 `data/`，并同时备份 `.env`、外部 Agent 自身数据和 microsandbox Volume。不要把含真实密钥、对话或附件的运行目录提交到 Git。
