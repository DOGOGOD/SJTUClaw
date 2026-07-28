# SJTUClaw `data` 目录说明

## 1. 概述

`data/` 不是用于训练模型的数据集，也不是项目源码的一部分。它是 SJTUClaw 的本地运行时持久化目录，相当于应用的“用户数据库”。

其中的数据主要来自用户操作、AI 对话、后台任务和程序自动生成。保存这些数据，是为了让会话、记忆、配置、定时任务和工作区状态在程序重启后仍然存在。

`data/` 已被 `.gitignore` 排除，正常情况下不会提交到 Git。这一点非常重要，因为其中可能包含：

- 用户与 AI 的对话正文；
- 用户偏好和项目背景；
- 上传的附件；
- 本地工作区绝对路径；
- 定时任务消息和历史输出；
- LLM API Key、QQ Client Secret 等敏感配置。

因此，整个 `data/` 目录都应被视为本地私有数据，不适合作为项目样例公开或发送给其他人。

## 2. 数据目录的位置

SJTUClaw 会根据运行方式决定数据目录：

- 源码运行：默认使用项目根目录下的 `data/`；
- 安装版运行：默认使用当前用户目录下的 `.sjtuclaw/data/`；
- 自定义位置：可通过环境变量 `SJTUCLAW_DATA_DIR` 指定。

相关逻辑位于 `claw/paths.py` 的 `data_dir()` 函数中。

当前项目中的主要结构如下：

```text
data/
├── cron/           # 定时任务及运行输出
├── memory/         # 跨会话长期记忆
├── pet/            # 桌宠运行设置和互动台词
├── pets/           # 用户导入的宠物资源
├── pi/             # Pi Agent 后端原生会话
├── sessions/       # SJTUClaw 主会话和附件
├── settings/       # Web UI 运行配置和密钥
└── workspace/      # 工作区绑定和文件回退数据
```

## 3. `data/sessions/`：SJTUClaw 主会话

### 3.1 存储什么

`sessions/` 保存 SJTUClaw 自己维护的权威会话历史，包括：

- 会话 ID 和标题；
- 创建、更新时间；
- 用户消息、助手消息和工具消息；
- 消息 ID 和时间戳；
- 会话摘要；
- 上下文压缩边界；
- 会话 revision；
- 当前 Agent 后端；
- Pi 会话映射信息；
- 回退检查点 ID；
- 中断恢复所需的状态；
- 与当前会话绑定的附件。

每个正式会话存储为一个 `.jsonl` 文件：

```text
sessions/
├── c2Vzc2lvbl8wMDE.jsonl
└── c2Vzc2lvbl8wMDI.jsonl
```

文件名是会话 ID 的 URL-safe Base64 编码。例如：

```text
session_001 → c2Vzc2lvbl8wMDE
session_002 → c2Vzc2lvbl8wMDI
```

编码文件名可以避免会话 ID 中的中文、空格、斜杠或其他特殊字符直接进入文件路径，从而减少路径冲突和安全问题。

### 3.2 文件格式

每个 JSONL 文件：

1. 第一行是会话元数据；
2. 后续每一行是一条消息。

元数据大致包括：

```json
{
  "_type": "metadata",
  "key": "session-id",
  "created_at": "...",
  "updated_at": "...",
  "metadata": {
    "title": "...",
    "agent_backend": "...",
    "summary": "...",
    "pi_session_generation": "..."
  },
  "last_consolidated": 0,
  "revision": 0
}
```

消息大致包括：

```json
{
  "role": "user",
  "content": "...",
  "timestamp": "...",
  "message_id": "..."
}
```

根据消息类型，还可能包含媒体、工具调用、回退检查点等字段。

### 3.3 数据从哪里来

这些数据主要来自：

- Web UI 中发送的消息；
- CLI 中发送的消息；
- QQ Bot 等外部渠道转发的消息；
- LLM 返回的回答；
- Agent 的工具调用过程；
- Scheduler 触发的 Agent 回合；
- 后台上下文压缩产生的摘要。

### 3.4 为什么使用 JSONL

JSONL 相比一个大型 JSON 文件有以下优点：

- 可以逐条追加；
- 单行损坏时不会导致整个会话完全无法读取；
- 更适合长会话；
- 更容易实现原子写入；
- 可以保存 compact 摘要和 revision；
- 有利于崩溃恢复和会话分叉。

### 3.5 会话附件

附件保存在：

```text
sessions/<session-id>/attachments/
├── .meta.json
├── att_xxxxxxxxxxxx.png
└── ...
```

`.meta.json` 记录附件 ID、原始文件名、内部文件名、文件大小、MIME 类型和上传时间等信息。

附件按会话隔离，一个会话不能通过附件工具读取另一个会话的附件。这既是功能边界，也是隐私和安全边界。

### 3.6 为什么要保存

如果不保存主会话：

- 程序重启后聊天记录会消失；
- Web UI 无法列出历史会话；
- AI 无法继续原有上下文；
- compact 摘要会丢失；
- 中断的回合难以恢复；
- 会话和工作区无法一起回退；
- 已上传的附件无法继续使用。

## 4. `data/pi/`：Pi Agent 后端数据

### 4.1 存储什么

`pi/` 的结构通常是：

```text
pi/
├── sessions/
│   ├── 2026-...jsonl
│   └── ...
└── runtime/
```

`pi/sessions/` 保存 Pi Agent 子进程自己的原生会话数据，包括：

- Pi session 初始化信息；
- Pi session ID；
- 工作目录；
- 模型提供方；
- 模型 ID；
- thinking level；
- 用户和助手消息；
- 工具调用过程；
- 父子事件关系；
- Pi 原生 compact 事件。

### 4.2 数据从哪里来

当 `AGENT_BACKEND` 选择 Pi 时，SJTUClaw 会启动 Pi RPC 子进程，并将 `data/pi/sessions/` 作为 Pi 的 session 目录。

每个 SJTUClaw 会话通过“会话 ID + generation”映射到一个 Pi session。发生回退、分叉或重建上下文时，generation 会更新，避免继续使用已经与主会话不一致的 Pi 历史。

### 4.3 与 `data/sessions/` 的区别

| `data/sessions/` | `data/pi/sessions/` |
|---|---|
| SJTUClaw 的权威会话记录 | Pi 后端的原生执行记录 |
| 用于 Web UI、CLI、摘要和回退 | 用于 Pi 恢复上下文和原生 compact |
| 格式由 SJTUClaw 控制 | 格式由 Pi Agent 控制 |
| 无论使用什么后端都需要 | 主要在使用 Pi 后端时生成 |

两者在内容上可能部分重叠，但用途不同，不能简单地将其中一个视为另一个的备份。

### 4.4 `pi/runtime/`

运行 Pi 时，SJTUClaw 会在 `pi/runtime/` 中短暂生成：

- 拼接后的 Prompt 文件；
- 工具清单 JSON；
- 当前回合的桥接数据。

这些文件通常在本轮结束后清理，因此该目录经常为空。它主要是临时交换区，不是长期业务数据。

## 5. `data/memory/`：跨会话长期记忆

### 5.1 存储什么

长期记忆使用独立 Markdown 文件保存：

```text
memory/
├── user_preference/
├── project/
├── fact/
├── decision/
├── general/
└── reflection_config.json
```

支持的记忆分类包括：

- `user_preference`：用户偏好；
- `project`：长期项目背景；
- `fact`：值得长期保留的事实；
- `decision`：已经做出的重要决定；
- `general`：其他长期信息。

每条记忆大致采用以下格式：

```yaml
---
id: "mem_001"
category: "project"
tags:
  - example
importance: 4
source_session_id: "session_001"
created_at: "..."
updated_at: "..."
---

记忆正文
```

### 5.2 数据从哪里来

长期记忆有两个主要来源：

1. Agent 在对话过程中调用记忆工具主动写入；
2. 每日 Reflection 后台任务回顾近期会话，由 LLM 提取值得长期保留的信息。

Reflection 会读取会话摘要和最近消息，尝试提取：

- 项目背景；
- 用户偏好；
- 已作出的决定；
- 长期有效的重要事实。

### 5.3 `reflection_config.json`

该文件保存自动反思任务的状态，例如：

- 是否启用；
- 每天的运行时间；
- 上次运行时间；
- 每次检查了多少会话；
- 提取了多少条记忆；
- 运行是否成功；
- 错误信息和历史记录。

### 5.4 为什么要保存

普通模型上下文存在长度限制，不可能永久携带全部聊天历史，而且不同会话默认相互独立。

长期记忆让助手能够跨会话记住稳定信息，例如：

- 用户习惯；
- 常用技术栈；
- 长期项目目标；
- 已确认的架构决定；
- 用户明确要求长期保留的事实。

使用独立 Markdown 文件还便于人工检查、修改、删除和迁移。

## 6. `data/cron/`：定时任务和运行输出

### 6.1 目录结构

```text
cron/
├── jobs.json
└── runs/
    └── <job-id>/
        └── YYYYMMDD_HHMMSS.md
```

### 6.2 `jobs.json`

`jobs.json` 是定时任务的权威存储，主要包含：

- 任务 ID；
- 任务名称；
- 是否启用；
- 触发方式；
- 指定时间、固定间隔或 cron 表达式；
- 时区；
- 交给 Agent 的消息；
- 绑定的会话；
- 来源渠道和目标聊天；
- 是否投递执行结果；
- 依赖的其他任务；
- 下次运行时间；
- 上次运行状态；
- 上次错误；
- 暂停原因；
- 重复次数；
- 已完成次数；
- Scheduler 心跳；
- 最近成功时间。

### 6.3 `runs/`

定时任务每次执行后的文本结果保存为：

```text
runs/<job-id>/<执行时间>.md
```

这些结果用于：

- 查看任务过去做了什么；
- 排查任务失败；
- 审计自动执行结果；
- 让后续任务读取前置任务的最新输出；
- 构建任务依赖链。

每个任务最多保留一定数量的输出文件，当前代码限制为 50 份，以避免长期运行后无限占用磁盘。

### 6.4 数据从哪里来

任务定义来自：

- 用户通过 Web UI 创建或修改任务；
- 用户通过命令或工具创建任务；
- 系统注册的内部定时任务。

运行输出来自 Scheduler 到期后触发的 Agent 回合。

### 6.5 为什么要保存

如果任务只存在内存中，Gateway 一旦重启：

- 所有任务都会消失；
- 无法计算下一次执行时间；
- 暂停和失败状态会丢失；
- 无法查看历史输出；
- 任务依赖无法读取前置结果。

## 7. `data/pet/`：桌宠运行状态

典型结构：

```text
pet/
├── settings.json
├── desktop.lock
└── replies/
    └── <pet-id>.json
```

### 7.1 `settings.json`

保存：

- 桌宠是否启用；
- 当前选择的宠物 ID；
- 是否随 Gateway 启动；
- 桌宠最后一次关闭时的屏幕坐标。

保存这些数据后，桌宠重启时可以恢复用户上次的选择和位置。

### 7.2 `desktop.lock`

这是桌宠进程锁，用于防止重复启动多个桌宠实例。

该文件通常没有正文，是临时运行状态。程序异常退出时可能留下；正常启动流程会根据锁状态判断是否已有桌宠在运行。

### 7.3 `replies/<pet-id>.json`

每只宠物的互动台词单独保存，字段通常包括：

- schema 版本；
- 宠物 ID；
- 宠物描述；
- 台词来源；
- 生成时间；
- 台词列表。

宠物导入后，程序会根据 `displayName` 和 `description` 调用 LLM 生成符合人设的互动台词。如果 LLM 未配置或生成失败，则保存通用备用台词。

台词不直接写回宠物包，而是按宠物 ID 单独保存。这样：

- 不会修改原始宠物资源；
- 删除宠物时可以单独删除其台词；
- 不会影响其他宠物；
- 可以重新生成台词而无需重新导入图片。

## 8. `data/pets/`：用户导入的宠物资源

用户导入的宠物通常保存为：

```text
pets/<pet-id>/
├── pet.json
└── spritesheet.png
```

或者：

```text
pets/<pet-id>/
├── pet.json
└── spritesheet.webp
```

`pet.json` 保存宠物 ID、显示名称、描述、精灵图版本和精灵图路径等信息。

内置宠物属于只读程序资源；用户导入的宠物属于可写用户数据。两者分开后：

- 安装和升级不会覆盖用户宠物；
- 用户宠物可以独立删除；
- 内置资源不会被意外修改；
- 桌宠目录可以进行严格的格式和安全校验。

## 9. `data/settings/`：运行设置和敏感配置

目录结构：

```text
settings/
├── runtime_settings.json
└── runtime_settings.key
```

### 9.1 `runtime_settings.json`

保存 Web UI 中修改的运行配置，例如：

- LLM Base URL；
- 模型名称；
- 上下文窗口；
- 最大输出 token 数；
- compact 比例；
- Agent 后端；
- QQ Bot 是否启用；
- QQ App ID；
- QQ 消息格式；
- 用户头像设置。

敏感字段包括：

- `LLM_API_KEY`；
- `COMPACT_LLM_API_KEY`；
- `QQ_CLIENT_SECRET`。

这些字段不会直接以明文字符串写入 JSON，而是保存为加密对象。

### 9.2 `runtime_settings.key`

该文件保存 Fernet 解密密钥。程序使用它加密和解密 `runtime_settings.json` 中的敏感字段。

### 9.3 数据从哪里来

初次运行可以从 `.env` 读取配置。用户通过 Web UI 修改配置后，新值会写入 `runtime_settings.json`，并优先于 `.env` 生效。

### 9.4 为什么要保存

- 让 Web UI 的修改在重启后继续生效；
- 避免每次修改都直接重写明文 `.env`；
- 使安装版升级时保留用户配置；
- 避免 API Key 在普通 JSON 字段中以明文显示。

需要注意：密钥文件和密文位于同一用户数据目录。这种设计主要防止敏感值直接明文暴露，不等同于操作系统级密钥保险库。

因此：

- 不应提交 `data/settings/`；
- 不应把整个 `data/` 打包公开；
- 排查问题时也不应直接上传其中的原始文件。

## 10. `data/workspace/`：工作区绑定和回退

目录结构：

```text
workspace/
├── bindings.json
└── rollback/
    ├── state.db
    ├── state.db-shm
    ├── state.db-wal
    └── objects/
```

### 10.1 `bindings.json`

该文件保存：

```text
session ID → workspace 绝对路径
```

这样 Gateway 重启后，每个会话仍然可以恢复到原来的工作目录。

这里只保存路径映射，不保存工作区文件本身。

### 10.2 `rollback/state.db`

这是 SQLite 数据库，主要包含：

- `bindings`：回退系统识别的会话、工作区和绑定代次；
- `checkpoints`：检查点、消息 ID、文件清单和会话快照；
- `operations`：回退、撤销、安全检查点、状态和错误。

回退系统不依赖工作区中的 Git 仓库。

### 10.3 `rollback/objects/`

存在检查点时，工作区文件内容按 SHA-256 哈希保存：

```text
objects/<哈希前两位>/<剩余哈希>
```

相同内容只保存一次，因此多个检查点之间可以共享对象，减少磁盘占用。

### 10.4 保存哪些回退信息

一个检查点通常需要同时记录：

- 工作区文件清单；
- 文件类型和权限信息；
- 每个文件对应的内容哈希；
- 当前完整会话快照；
- 目标消息 ID；
- 父检查点；
- 检查点状态；
- 是否属于部分回退；
- 创建时间。

这样 `/rollback` 可以把“工作区文件”和“对话状态”同时恢复到某一用户回合之前。

### 10.5 `state.db-shm` 和 `state.db-wal`

这是 SQLite 在 WAL 模式下自动创建的辅助文件：

- `state.db-wal`：尚未合并回主数据库的事务日志；
- `state.db-shm`：多个连接协调使用的共享内存索引。

它们不是独立业务文件，不应在程序运行期间手动删除或编辑。

## 11. 为什么统一存放在 `data/`

### 11.1 重启后保持状态

如果不落盘，程序关闭后会丢失会话、设置、任务、记忆、桌宠位置和工作区绑定。

### 11.2 分离源码和用户数据

`claw/`、`prompts/`、内置 `skills/` 和内置宠物属于程序资源；`data/` 属于运行过程中产生的用户数据。

这种分离允许安装版升级程序时保留用户数据。

### 11.3 多入口共享状态

Web UI、CLI、QQ Bot、Scheduler 和桌宠可以通过同一套数据目录共享：

- 会话；
- 配置；
- 任务；
- 记忆；
- 工作区状态。

### 11.4 会话隔离

附件、工作区、Pi session 和回退点都与 session ID 关联，避免不同会话互相读取不属于自己的数据。

### 11.5 恢复和审计

JSONL 会话、定时任务输出、SQLite 检查点和内容对象库让系统能够支持：

- 崩溃恢复；
- 历史检查；
- 文件回退；
- 回退撤销；
- 自动任务审计；
- 长会话压缩。

### 11.6 隐私保护

`data/` 不属于可公开的代码资源。将其统一放入被 Git 忽略的目录，可以降低聊天内容、用户偏好、工作区路径和密钥误提交的风险。

## 12. 删除不同目录的影响

| 删除对象 | 主要影响 |
|---|---|
| `data/sessions/` | 丢失聊天历史、摘要、消息状态和附件 |
| `data/pi/` | Pi 原生上下文和 compact 记录丢失，后续可能需要重新交接上下文 |
| `data/memory/` | 助手不再保留已提取的长期记忆 |
| `data/cron/` | 定时任务及历史输出丢失 |
| `data/pet/` | 桌宠选择、位置和互动台词被重置 |
| `data/pets/` | 用户导入的宠物资源丢失 |
| `data/settings/` | Web UI 配置和加密凭据丢失，需要重新配置 |
| `data/workspace/` | 工作区绑定和文件回退检查点丢失 |
| 整个 `data/` | 应用基本恢复为首次运行状态，但所有本地用户数据都会丢失 |

不建议在 Gateway、Scheduler、桌宠或其他 SJTUClaw 进程运行时手动修改或删除这些文件。

## 13. 总结

`data/` 保存的不是“训练 SJTUClaw 的数据”，而是：

> 这个 SJTUClaw 实例经历过什么、记住了什么、配置成什么、下一步需要做什么，以及如何恢复过去的状态。

其中：

- `sessions/` 负责对话；
- `pi/` 负责 Pi 后端上下文；
- `memory/` 负责长期记忆；
- `cron/` 负责自动任务；
- `pet/` 和 `pets/` 负责桌宠；
- `settings/` 负责运行配置；
- `workspace/` 负责工作区绑定和回退。

这些数据共同构成 SJTUClaw 的持久化运行状态。
