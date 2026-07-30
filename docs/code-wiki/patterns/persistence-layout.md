# Persistence Layout

> Persistence Layout 把只读应用资源、可写用户数据、Session 事实和外部运行时状态明确分开。

## 路径决策

`claw/paths.py` 是路径事实源。

### 源码运行

```text
resource_root = 仓库根目录
main_dir      = 仓库根目录
data_dir      = <repo>/data
env_path      = <repo>/.env
web_dir       = <repo>/web
prompts_dir   = <repo>/prompts
skills_dir    = <repo>/skills
```

无论从哪个 Shell 目录启动 Gateway，源码版的默认 Agent 主目录都固定为仓库根。

### PyInstaller 安装版

```text
resource_root = sys._MEIPASS
user_root     = %USERPROFILE%\.sjtuclaw
main_dir      = user_root
data_dir      = user_root/data
env_path      = user_root/.env
web_dir       = 打包资源 web/
```

安装版首次访问 Prompt / Skill 时，会把打包的只读资源复制到 `data/prompts` 和 `data/skills`，之后允许用户修改。

### 覆盖

```text
SJTUCLAW_USER_DIR
SJTUCLAW_DATA_DIR
```

前者改变用户根，后者只改变 Data Root。

## Data Tree

```text
data/
├── sessions/
│   ├── <encoded-id>.jsonl
│   └── <session-id>/attachments/
├── memory/
│   ├── <category>/*.md
│   └── reflection_config.json
├── cron/
│   ├── jobs.json
│   └── runs/<job-id>/*.md
├── workspace/
│   ├── bindings.json
│   └── rollback/
│       ├── state.db
│       └── objects/
├── settings/
│   ├── runtime_settings.json
│   └── runtime_settings.key
├── downloads/registry.json
├── sandbox/exports/
├── runtime/microsandbox/
├── pet/
│   ├── settings.json
│   └── replies/*.json
├── pets/<pet-id>/
├── pi/
│   ├── sessions/
│   └── runtime/
└── claude/runtime/
```

不是所有目录都会同时出现。

## Session Store

### 文件名

Session ID 经校验后使用 URL Safe Base64 编码，避免用户 ID 与文件路径直接耦合。

### 文件内容

```text
line 1: metadata
line 2+: message
```

元数据同时保存：

- 标题
- Summary
- `last_consolidated`
- Agent Backend
- AUTO / Sandbox 偏好
- 外部 Session Generation
- 运行恢复标记

Message 逐行保存 Tool Call、Tool Result、Media 路径和回退锚点。

### 恢复标记

`runtime_checkpoint` 可以保存当前未闭合 Assistant Tool Call 与已获得的结果。启动时补回这些 Message，避免崩溃造成工具配对断裂。

`pending_user_turn` 标记在用户消息持久化边界异常时帮助恢复或清理重复。

### 文件上限

Session 模型与 Store 的消息上限为 2000。Context Replay 上限还会根据模型 Context Window 估算，至少保留可用的近期窗口。

## Attachments

```text
data/sessions/<session-id>/attachments/
├── .meta.json
└── <random-name>.<safe-suffix>
```

`.meta.json` 保存 Attachment ID、原始名、保存名、类型和大小。随机文件名避免原始文件名直接控制路径。

Session JSONL 的 `media` 字段引用实际附件路径。删除附件会使历史图片无法再次编码给模型。

## Memory

```text
data/memory/<category>/<slug>.md
```

Front Matter 保存结构字段，正文保存内容。Memory ID 和文件名不是完全等价：文件名来自正文 Slug，并在冲突时唯一化；逻辑查找使用 `memory_id`。

旧 `memory.json` 只在没有 Markdown 条目时迁移，避免新旧数据合并产生重复。

## Cron

`jobs.json` 是整个 Cron Store 的原子快照。运行输出按 Job 分目录，文件名使用本地配置时区格式化时间。

输出数量按 Job 修剪。Job Dependency 只读取最新文件，因此旧运行记录的清理不会破坏当前依赖语义。

## Workspace

### Binding

`bindings.json` 是简单 Session → 绝对路径映射。每次写入都在跨进程文件锁内重读，合并单项修改，再原子替换。

### Rollback

SQLite `state.db` 保存：

- `bindings`
- `checkpoints`
- `operations`

启用 WAL 与 Foreign Key。对象内容放在 `objects/`，以 SHA-256 命名并压缩。

数据库保存 Manifest 与 Conversation Snapshot，Object Store 保存文件字节；两者配合才能恢复。

## Runtime Settings

`runtime_settings.json` 是环境变量风格的扁平 Key Map。

敏感项：

```text
LLM_API_KEY
COMPACT_LLM_API_KEY
QQ_CLIENT_SECRET
```

保存时敏感值变成：

```json
{"encrypted": "<fernet-token>"}
```

读取 API 可以选择返回明文或统一掩码。`setting_value()` 优先读取 Runtime Settings，没有对应 Key 时再读取环境变量。

Key 文件创建与 Settings 写入都使用文件锁、唯一临时文件、Flush、`fsync` 和替换。

## Download Registry

```text
data/downloads/registry.json
```

内存结构保存：

```text
download_id → (absolute_path, registration_order)
```

每次列表 / 查询前可以重新加载和修剪：

- 源文件不存在则移除
- 超过 1000 条则移除最旧
- 受管 Sandbox Export 随注册项淘汰清理

Download ID 有格式校验，Gateway 不接受路径参数。

## Pet

```text
data/pet/settings.json
data/pet/replies/<pet-id>.json
data/pets/<pet-id>/pet.json
data/pets/<pet-id>/spritesheet.webp
```

内置宠物从应用资源读；用户宠物写 Data Root。删除用户宠物时同步删除其角色台词，但不影响其他宠物。

桌宠窗口位置写回 `settings.json`，独立进程重启后恢复。

## Pi

```text
data/pi/sessions/
data/pi/runtime/
```

- `sessions/`：Pi CLI 自己管理的原生 transcript。
- `runtime/`：每回合 Prompt 和桥接文件，结束后尽力删除。

`PI_SESSION_DIR` 可以把原生 transcript 移到其他位置。

## Claude

```text
data/claude/runtime/
```

这里保存短生命周期：

- Append Prompt
- Claude Settings
- Host Tool Manifest
- MCP Config
- Approval Relay 文件

Claude 原生 transcript 由系统 Claude Code 自己保存，不在 SJTUClaw Data Root 中。

## Sandbox

```text
data/runtime/microsandbox/
data/sandbox/exports/
```

第一个目录用于 GUI / 打包环境中的 `msb` 运行时准备；第二个目录用于 guest 文件导出。

真正的镜像与私有 Volume 由 microsandbox `MSB_HOME` 管理，通常在用户目录 `.microsandbox`，不属于 SJTUClaw Backup。

## 原子写入模式

项目在多个 Store 中重复使用：

```text
获取实例锁
→ 获取 FileLock
→ 读取最新磁盘状态
→ 写唯一临时文件
→ flush
→ 可选 fsync
→ os.replace
→ 清理临时文件
→ 发布内存缓存
```

唯一临时文件名包含 PID 或 UUID，避免多个进程争用固定 `.tmp`。

Windows 下 `os.replace` 可能被杀毒软件或索引器短暂阻止；Session 和 Workspace Store 对明确的短暂错误做有限退避，不吞掉永久 Permission Error。

## 数据所有权

| 数据 | 所有者 | 卸载行为 |
| --- | --- | --- |
| `web/`、内置 Prompt / Skill / Pet | 应用资源 | 随应用删除 |
| `.sjtuclaw/data` | 用户 | 安装程序不主动删除 |
| `.env` | 用户 | 安装程序不主动删除 |
| Pi / Claude 原生数据 | 外部 Agent | SJTUClaw 不删除 |
| microsandbox 镜像 / Volume | microsandbox | SJTUClaw 不删除 |
| 绑定 Workspace | 用户项目 | SJTUClaw 只修改 Agent 实际操作涉及的文件 |

## 备份一致性

最可靠方式：

1. 停止 Gateway、CLI、TUI 和桌宠。
2. 备份整个 Data Root 与 `.env`。
3. 如使用 Pi，备份自定义 `PI_SESSION_DIR`。
4. 按 microsandbox 文档备份所需 Volume。
5. 保留 Runtime Settings JSON 与 Key 的配对。

只备份 `sessions/` 不能恢复 Memory、Cron、回退、Workspace 绑定或模型设置。

## 相关页面

- [[concepts/session-context]]
- [[patterns/security-boundaries]]
- [[concepts/memory-skill-scheduler]]
- [[products/windows-distribution]]

## 源码依据

- `claw/paths.py`
- `claw/config.py`
- `claw/session/store.py`
- `claw/memory/store.py`
- `claw/scheduler/service.py`
- `claw/workspace/manager.py`
- `claw/workspace/rollback.py`
- `claw/runtime_settings.py`
- `claw/tools/download.py`
- `claw/pet/catalog.py`
- `claw/pet/replies.py`
- `claw/pi/client.py`
- `claw/claude/client.py`
- `claw/sandbox/runtime.py`
