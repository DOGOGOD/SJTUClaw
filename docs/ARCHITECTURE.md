# SJTUClaw 架构说明

## 总体架构

```
┌──────────┐  ┌───────────┐  ┌───────────┐
│  CLI入口  │  │  Gateway  │  │ Scheduler │
│ (repl.py) │  │(server.py)│  │(定时触发) │
└─────┬─────┘  └─────┬─────┘  └─────┬─────┘
      │              │              │
      └──────────────┼──────────────┘
                     │
              ┌──────▼──────┐
              │ Agent Loop  │  ← 唯一 LLM 入口
              │ (loop.py)   │    run_agent_turn()
              └──────┬──────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
   ┌────▼───┐  ┌─────▼──────┐ ┌─▼──────────┐
   │Context │  │   Tool     │ │  Approval  │
   │Builder │  │  Registry  │ │  Manager   │
   └────┬───┘  └─────┬──────┘ └────────────┘
        │            │
   ┌────┼────┐  ┌────┼────────────┐
   │    │    │  │    │            │
  SP  Soul Mem │ R/O Update Shell │
               │ Tool  Tool  Tool │
               │           │      │
               │      ┌────▼───┐  │
               │      │Workspace│  │
               │      │Manager │  │
               │      └────────┘  │
               │            │     │
               └────────────┼─────┘
                            │
                     ┌──────▼──────┐
                     │   Session   │
                     │   Store     │
                     │ (持久化)    │
                     └──────┬──────┘
                            │
                     ┌──────▼──────┐
                     │   data/     │
                     │  (JSON文件) │
                     └─────────────┘
```

## 核心原则

**单一入口**: `run_agent_turn()` 是唯一调用 LLM 的入口。CLI/Gateway/Scheduler/Skill 都通过它工作，不能绕过。

## 模块职责边界

### agent/loop.py — Agent Loop（核心编排器）
- `run_agent_turn(session_id, user_message, ...)`: 唯一 agent 入口
- 内部循环: buildContext → callLLM → tool_calls → execute → 直到 final
- 负责拦截 `use_skill`（skill_select）、write/shell tool 的 approval 流程
- 不依赖任何前端（CLI/Web），只接收回调函数

### context/builder.py — 上下文组装
- 拼装顺序: system prompt → soul → memory → tool协议 → skill索引 → session summary → session messages
- 稳定上下文（SP/soul/memory）与对话上下文（session messages）严格分离
- skill 索引只在 builder 中注入轻量信息（name+description），不含完整 instructions

### context/compaction.py — 压缩
- 触发阈值: 消息数>12 或 总字符>4000（且消息数>6）
- 只处理 session messages，不动 stable context
- 失败保护: 先计算 summary 再应用，失败时旧消息不丢

### session/store.py — Session 存储
- 每 session 一个 JSON 文件: `data/sessions/<id>/session.json`
- 支持 CRUD + 持久化，损坏文件备份为 `.corrupted-*`

### tools/base.py — Tool 抽象
- `Tool`: name + description + input_schema + handler + safety_level
- `ToolRegistry`: register/list_definitions/execute_by_name
- 执行时校验参数，不信任模型输出

### tools/readonly.py — 只读工具
- `current_time`, `list_dir`, `read_file`
- safety_level = "read_only"

### tools/update.py — 文件修改工具
- `create_file`, `overwrite_file`, `edit_file`
- safety_level = "write"（需 approval）
- 所有路径通过 workspace.resolve() 校验

### tools/shell.py — Shell 工具
- `new_shell`, `run_command`
- safety_level = "shell"（需 approval）
- 跨平台：Windows 用 cmd.exe，POSIX 用 /bin/sh
- Unix→Windows 自动命令翻译
- cwd 通过状态文件持久化，执行前后双重边界检查

### tools/download.py — 下载工具
- `create_download`: 为 workspace 文件注册下载入口
- 不需要 approval（用户确认在前端点击时）

### tools/skills.py — Skill 选择工具
- `use_skill`: 内部工具，由 agent loop 拦截处理
- safety_level = "skill_select"

### workspace/manager.py — 工作区管理
- per-session 绑定，持久化到 `data/workspace/<id>/workspace.json`
- 路径解析: 相对路径 → workspace 内，拒绝 `../` 和绝对路径越界
- 未设置时所有写操作直接失败

### approval/manager.py — 审批
- `create/approve/reject/wait`: 线程安全的审批管理
- Threading.Event 实现阻塞等待
- CLI 用内联子循环，Gateway 用 REST 端点触发

### skills/registry.py — Skill 系统
- 扫描 `skills/` 目录，解析 SKILL.md frontmatter
- `list_index()`: 轻量索引（仅 name+description）
- `load_skill(name)` / `format_full_content(name)`: 完整内容
- Skill 使用记录写入 session.skill_usage

### gateway/server.py — HTTP 服务
- 根路由 `/` → 直接 serve WebUI
- REST API: /chat, /sessions, /attachments, /workspace, /approvals, /downloads, /skills, /tasks
- 中间件: CORS 全开放

### scheduler/ — 定时任务
- 支持一次性(once) + 固定间隔(interval) + 每天定时(daily)
- 后台线程轮询，复用 run_agent_turn

### cli/ — 命令行界面
- `repl.py`: 交互循环 + 审批子循环
- `commands.py`: /session, /memory, /compact, /workspace, /approve, /reject, /skill 解析

## 消息流

```
用户输入 (CLI/Gateway/Scheduler)
    → run_agent_turn(session_id, user_message)
        → session.append_message("user", ...)
        → while True:
            → context_builder.build_messages(session)
                [SP + soul + memory + tools + skills_index + summary + msgs]
            → llm_client.chat_with_tools(messages, tool_defs)
            → if final: return
            → if use_skill:
                → approval_handler → 用户确认
                → 注入 skill 完整内容 → 继续
            → if write/shell:
                → approval_handler → 用户确认/拒绝
                → 执行 tool → 结果写入 session
            → if read_only/download:
                → 直接执行 → 结果写入 session
            → continue loop
        → return assistant reply
```

## 数据流

```
data/
├── sessions/<sessionId>/
│   ├── session.json          # Session 数据 (messages + summary + skill_usage)
│   └── attachments/          # 附件文件 + .meta.json
├── memory/memory.json        # 跨 session 长期记忆
├── tasks/tasks.json          # 定时任务
└── workspace/<sessionId>/
    └── workspace.json        # workspace 绑定
```
