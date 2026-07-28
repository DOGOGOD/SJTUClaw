# SJTUClaw Code Wiki

> 本文档是 SJTUClaw 项目的结构化代码百科，覆盖项目整体架构、模块职责、关键类与函数、依赖关系、运行方式，并对 `SJTUClaw.md` 中要求的所有功能以及开发者新增功能给出具体代码位置与实现阐释。

---

## 目录

- [一、项目概览](#一项目概览)
- [二、整体架构](#二整体架构)
- [三、模块职责详解](#三模块职责详解)
- [四、关键类与函数说明](#四关键类与函数说明)
- [五、依赖关系](#五依赖关系)
- [六、项目运行方式](#六项目运行方式)
- [七、SJTUClaw.md 功能要求实现说明](#七sjtuclawmd-功能要求实现说明)
  - [7.1 多 Session 管理与持久化（Step 2）](#71-多-session-管理与持久化step-2)
  - [7.2 Agent Loop（Step 1、5、8）](#72-agent-loopstep-158)
  - [7.3 Context Build（Step 2、3、4、5）](#73-context-buildstep-2345)
  - [7.4 Compaction 上下文压缩（Step 4）](#74-compaction-上下文压缩step-4)
  - [7.5 Memory 系统（Step 3）](#75-memory-系统step-3)
  - [7.6 Tool 系统（Step 5、8）](#76-tool-系统step-58)
  - [7.7 Gateway 与 WebUI（Step 6）](#77-gateway-与-webuistep-6)
  - [7.8 Cron 定时任务系统（Step 7）](#78-cron-定时任务系统step-7)
  - [7.9 Workspace（Step 8）](#79-workspacestep-8)
  - [7.10 Skill 系统（Step 9）](#710-skill-系统step-9)
- [八、开发者新增功能实现说明](#八开发者新增功能实现说明)
  - [8.1 Pet 桌面宠物](#81-pet-桌面宠物)
  - [8.2 Rollback 工作区回退](#82-rollback-工作区回退)
  - [8.3 Reflect 每日反思](#83-reflect-每日反思)
  - [8.4 Auto 与 Unlimited 模式](#84-auto-与-unlimited-模式)
  - [8.5 Pi Agent 接入](#85-pi-agent-接入)
- [九、核心运行机制与模块关系](#九核心运行机制与模块关系)
  - [9.1 状态归属与隔离总表](#91-状态归属与隔离总表)
  - [9.2 Session 隔离](#92-session-隔离)
  - [9.3 Agent Loop](#93-agent-loop)
  - [9.4 Compaction 系统](#94-compaction-系统)
  - [9.5 Memory 系统](#95-memory-系统)
  - [9.6 Tool 系统](#96-tool-系统)
  - [9.7 Workspace 系统](#97-workspace-系统)
  - [9.8 Skill 系统](#98-skill-系统)
  - [9.9 Cron 系统](#99-cron-系统)
  - [9.10 Gateway](#910-gateway)
  - [9.11 Rollback](#911-rollback)
  - [9.12 AUTO 与 UNLIMITED 模式](#912-auto-与-unlimited-模式)
  - [9.13 Pi 后端切换](#913-pi-后端切换)
  - [9.14 端到端流程与模块协作](#914-端到端流程与模块协作)
- [十、测试体系](#十测试体系)

---

## 一、项目概览

SJTUClaw 是面向个人与教学场景的本地 AI Agent Runtime。它将多轮对话、工具调用、长期记忆、Skill、定时任务和桌面宠物整合为一个可扩展的 Agent 工作台，并提供 Windows 桌面应用、CLI、Web UI、REST API、QQ Bot 等多种入口。

**核心特征：**

- **统一主对话入口**：CLI / Web UI / QQ Bot / Heartbeat / Cron 的完整 turn 共享 `run_agent_turn()`；摘要、反思、标题等辅助 LLM 任务有各自的受控调用链。
- **可选 Pi Agent 后端**：通过 JSONL RPC 接入 Pi 编码 Agent，按 session 切换后端。
- **安全审批**：写入/Shell 等高风险工具必须审批，AUTO/UNLIMITED 模式按 session 隔离生效。
- **三层 Token 安全网**：`ContextBudget.check_overflow` → `ContextGovernor._snip_history` → `compaction.maybe_consolidate_by_tokens`。
- **Workspace 回退**：基于 SHA-256 内容寻址对象存储 + SQLite 元数据，原子性恢复文件与对话状态。
- **本地化**：自动识别系统时区，全链路 UTF-8 强制处理 Windows 中文编码问题。

**技术栈速览：**

| 层次 | 技术 |
|------|------|
| 后端 | Python 3.11、FastAPI、Uvicorn |
| LLM | OpenAI 兼容 API（openai SDK、httpx、aiohttp） |
| Agent | 自研 Agent Loop、ToolRegistry、ContextBuilder、CompactionWorker、ApprovalManager |
| 存储 | JSONL Session、SQLite 回退元数据、SHA-256 对象库、Markdown + YAML 记忆 |
| 调度 | croniter、Heartbeat 后台线程 |
| 前端 | React 18、TypeScript、Vite、Tailwind CSS、react-markdown、KaTeX、react-syntax-highlighter |
| 通道 | Windows 桌面应用（pywebview）、CLI、Web UI、REST API、QQ Bot WebSocket |
| 打包 | PyInstaller、Inno Setup 7、tkinter、Pillow |
| 测试 | pytest、Vitest |

---

## 二、整体架构

### 2.1 分层架构图

```text
┌────────────────────────────────────────────────────────────────────┐
│                          入口层 (Entry Layer)                       │
│  ┌──────────┐  ┌────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   CLI    │  │  Desktop   │  │   Gateway    │  │   QQ Bot     │ │
│  │ (repl)   │  │ (pywebview)│  │  (FastAPI)   │  │  (channels)  │ │
│  └────┬─────┘  └─────┬──────┘  └──────┬───────┘  └──────┬───────┘ │
└───────┼──────────────┼────────────────┼─────────────────┼─────────┘
        │              │                │                 │
        v              v                v                 v
┌────────────────────────────────────────────────────────────────────┐
│                       Agent Runtime 核心层                          │
│                                                                    │
│   ┌──────────────────────────────────────────────────────────────┐ │
│   │      run_agent_turn()  ← 主对话 turn 的统一入口               │ │
│   │      位置: claw/agent/loop.py                                │ │
│   └──────────────────────────────────────────────────────────────┘ │
│                             │                                      │
│       ┌─────────────────────┼─────────────────────┐                │
│       v                     v                     v                │
│  ┌──────────┐         ┌──────────┐          ┌──────────┐           │
│  │  Context │         │   Tool   │          │ Approval │           │
│  │ Builder  │         │ Registry │          │ Manager  │           │
│  └────┬─────┘         └────┬─────┘          └────┬─────┘           │
│       │                    │                     │                 │
│       v                    v                     v                 │
│  ┌──────────┐         ┌──────────┐          ┌──────────┐           │
│  │Compaction│         │   LLM    │          │ Workspace│           │
│  │  Worker  │         │  Client  │          │ Manager  │           │
│  └──────────┘         └──────────┘          └──────────┘           │
└────────────────────────────────────────────────────────────────────┘
        │                     │                     │
        v                     v                     v
┌────────────────────────────────────────────────────────────────────┐
│                          存储层 (Storage)                           │
│  ┌──────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
│  │ SessionStore │  │MemoryStore │  │ SkillReg.  │  │ RollbackDB │ │
│  │  (JSONL)     │  │ (Markdown) │  │ (SKILL.md) │  │ (SQLite)   │ │
│  └──────────────┘  └────────────┘  └────────────┘  └────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 一次对话请求的数据流

```text
用户输入 (CLI / HTTP / QQ)
   │
   ├─ 如果是斜杠命令 → cli/commands.handle_command 直接处理（不进入 LLM）
   │
   └─ 普通消息
        │
        v
   run_agent_turn(session_id, user_message, ...)
        │
        ├─ 若有 rollback_manager: turn_guard 创建 workspace 检查点
        │
        ├─ RuntimeAgentClient 按 session.metadata.agent_backend 分流
        │     ├─ sjtuclaw → 原生 Think-Act-Observe 循环
        │     └─ pi       → Pi JSONL RPC 完整 turn
        │
        ├─ 原生后端 while True (Think-Act-Observe 循环):
        │     │
        │     ├─ context_builder.build_messages(session, tool_registry)
        │     │     → 装配 system_prompt + soul + memory + skill_index
        │     │       + summary + recent messages
        │     │
        │     ├─ llm_client.chat_with_tools(messages, tool_defs)
        │     │
        │     ├─ if response.is_final:
        │     │     return _finish_reply(response.final)
        │     │
        │     ├─ if use_skill (skill_select):
        │     │     创建 skill approval → 用户确认 → 注入 skill 内容
        │     │
        │     ├─ if write / shell tool:
        │     │     创建 approval → 等待用户决定 → 执行或记录拒绝
        │     │
        │     ├─ if download / read_only / network:
        │     │     直接执行 → 写入 session 历史
        │     │
        │     └─ continue
        │
        ├─ _finish_reply:
        │     ├─ _maybe_async_compact: 触发后台压缩
        │     ├─ _emit_event(FinalEvent): 通知前端
        │     └─ 更新 metrics + health 监控
        │
        └─ 返回 assistant 文本
```

### 2.3 目录结构

```text
SJTUClaw/
├── claw/                         # Python 主程序
│   ├── agent/                    # Agent Loop、预算、事件与健康监控
│   ├── approval/                 # 高风险工具审批管理
│   ├── channels/                 # 外部渠道（QQ Bot）
│   ├── cli/                      # CLI 入口、REPL 与命令解析
│   ├── context/                  # Context Builder、Compact、治理与 Token 预算
│   ├── gateway/                  # FastAPI Gateway、REST API、SSE 与上传服务
│   ├── llm/                      # OpenAI 兼容客户端与协议适配
│   ├── memory/                   # 长期记忆存储与每日 Reflection
│   ├── pet/                      # 桌面宠物进程、状态与资源管理
│   ├── pi/                       # 可选 Pi Agent RPC 客户端与工具桥接
│   ├── prompts/                  # Prompt 模板与加载器
│   ├── scheduler/                # Cron、Heartbeat、任务分发与状态持久化
│   ├── session/                  # Session/Message 模型、标题与 JSONL Store
│   ├── skills/                   # Skill Registry、安装、统计与状态管理
│   ├── tools/                    # 文件、Shell、网页、附件、Memory、Cron、Skill 工具
│   ├── workspace/                # Workspace 绑定、边界检查与回退
│   ├── config.py                 # 配置加载与运行时入口配置
│   ├── runtime_settings.py       # Web UI 可写设置与敏感配置加密持久化
│   ├── desktop.py                # Windows 桌面壳，启动本地 Gateway 与 pywebview
│   ├── paths.py                  # 源码版 / PyInstaller 版 / 安装版路径切换
│   ├── main.py                   # CLI 主入口（启动 REPL + Cron + Reflection + Pet）
│   └── utils.py                  # 通用工具函数
├── prompts/                      # identity、system_prompt、soul、tool_contract
├── skills/                       # 内置 Skill 目录（course-report 等）
├── webui/                        # React + TypeScript + Vite 前端源码
├── web/                          # 已构建的 Web UI 静态产物
├── packaging/windows/            # PyInstaller spec + Inno Setup iss
├── docs/                         # 配置、测试、打包文档
├── tests/                        # pytest 测试套件
├── pyproject.toml                # 项目元数据与 CLI 入口
├── requirements.txt              # Python 依赖
├── .env.example                  # 环境变量模板
├── SJTUClaw.md                   # 课程任务说明
└── 中期报告.md
```

---

## 三、模块职责详解

### 3.1 `claw/agent/` — Agent 主循环

| 文件 | 职责 |
|------|------|
| `loop.py` | **主对话 turn 的统一入口**。实现 Think-Act-Observe 循环、skill 选择、审批门、停滞检测、健康告警和完整后端分流。 |
| `turn_context.py` | `TurnContext` dataclass，打包单轮所需状态（budget、metrics、rejection_tracker）。 |
| `budget.py` | 线程安全的 `IterationBudget`，限制单 turn 迭代次数。 |
| `events.py` | 5 类事件 dataclass：`ThinkingEvent`、`ToolCallStartEvent`、`ToolCallEndEvent`、`FinalEvent`、`ErrorEvent`，驱动 SSE。 |
| `metrics.py` | `TurnMetrics` + `TurnMetricsAggregator`，聚合诊断指标。 |
| `health.py` | `LoopHealthMonitor`，检测 LLM 失败率、工具异常等健康问题。 |

### 3.2 `claw/session/` — 会话存储

| 文件 | 职责 |
|------|------|
| `models.py` | `Message`、`Session` dataclass，支持原生 tool_calls 持久化、历史回放（token 预算截断）、文件数上限归档。 |
| `store.py` | `SessionStore`：JSONL 持久化、Base64 URL-safe 文件名、双层锁（RLock + FileLock）、原子写入、fork_session_before_user_index、runtime_checkpoint 崩溃恢复。 |
| `title.py` | `generate_session_title` + `auto_title_if_first_turn`：LLM 自动生成 ≤15 字中文标题。 |

### 3.3 `claw/memory/` — 长期记忆

| 文件 | 职责 |
|------|------|
| `store.py` | `MemoryStore`：Markdown + YAML frontmatter 文件存储；多维度加权检索（标签 + 内容 + CJK 字符 + 频率 + 时效）；5 类 category。 |
| `reflection.py` | `ReflectionManager`：后台线程每日扫描会话，LLM 提取结构化事实自动入记忆。 |

### 3.4 `claw/context/` — 上下文管理

| 文件 | 职责 |
|------|------|
| `builder.py` | `ContextBuilder`：负责组装原生主循环发往 LLM 的 messages 数组。装配顺序：identity → soul → memory block → tool contract → skill index → summary → recent messages；Pi 和辅助 LLM 任务有各自的受控 prompt 路径。 |
| `compaction.py` | 历史压缩核心：`compact_session`、`compact_session_snapshot`、`maybe_consolidate_by_tokens`。失败不丢消息。 |
| `compaction_worker.py` | `CompactionWorker`：仅在 token 阈值到达后执行后台压缩，revision 守卫防止 ABA 问题。 |
| `budget.py` | `ContextBudget`：不可变 token 预算快照，>105% 抛 `ContextOverflowError`。 |
| `governance.py` | `ContextGovernor`：发送前 8 步修复流水线（去占位、补缺失 tool result、截断、实时压缩）。 |
| `token_counter.py` | 全代码库 token 估算唯一真相源：tiktoken `o200k_base` + CJK 启发式回退。 |

### 3.5 `claw/tools/` — 工具系统

| 文件 | 职责 |
|------|------|
| `base.py` | `Tool` / `ToolResult` / `ToolRegistry` / `ToolGuardrails`；ContextVar 绑定 per-turn 上下文；JSON Schema 参数校验。 |
| `readonly.py` | `current_time` / `list_dir` / `read_file`（safety_level=read_only）。 |
| `update.py` | `create_file` / `overwrite_file` / `edit_file`（safety_level=write）。 |
| `shell.py` | `new_shell` / `run_command`（safety_level=shell），跨平台 + cwd 持久化 + 越界预检。 |
| `download.py` | `create_download`（safety_level=download），内存注册表 + Gateway 下载入口。 |
| `attachment.py` | `copy_attachment_to_workspace`（safety_level=write），强 session 隔离。 |
| `memory_tools.py` | `remember`（write）/ `recall`（read_only）桥接 MemoryStore。 |
| `cron_tool.py` | `CronTool`（read_only 但有副作用）：add/list/remove 定时任务。 |
| `skill_manager_tool.py` | `skill_manage`（write）：LLM 驱动创建/编辑/删除 skill。 |
| `skills_tool.py` | `skills_list` / `skill_view`（read_only）：渐进式披露 skill 内容。 |
| `web.py` | `web_fetch` / `web_search`（safety_level=network）：SSRF 防护 + 多搜索后端。 |

### 3.6 `claw/gateway/` — HTTP 网关

| 文件 | 职责 |
|------|------|
| `server.py` | Gateway 核心枢纽：FastAPI app、模块级单例、REST 路由、SSE 流、QQ 桥接、审批 HTTP 端点。 |
| `middleware.py` | 4 个中间件：Security（Token）、RateLimit（滑动窗口）、RequestSize（chunk 计数）、RequestLogging。 |
| `uploads.py` | `save_upload_limited`：流式 chunk 写入 + 超限回滚。 |
| `__main__.py` | Gateway 启动入口，监听非本机地址时强制要求 `GATEWAY_API_TOKEN`。 |

### 3.7 `claw/scheduler/` — 定时任务

| 文件 | 职责 |
|------|------|
| `service.py` | `CronService` 支持 at/every/cron 三种调度、run_claim at-most-once、依赖注入、输出持久化。 |
| `dispatcher.py` | `create_cron_dispatcher`：返回 dispatch 闭包，通过 hooks 解耦渠道差异。 |
| `callbacks.py` | `HeartbeatCallback`：扫描 `workspace/HEARTBEAT.md` 活动任务并触发 agent loop。 |
| `types.py` | `CronJob` / `CronSchedule` / `CronPayload` / `CronRunRecord` / `CronStore` dataclass。 |
| `session_turns.py` | `visible_session_messages`：隐藏 cron 触发提示，序列化用户可见历史。 |

### 3.8 `claw/workspace/` — 工作区

| 文件 | 职责 |
|------|------|
| `manager.py` | `WorkspaceManager`：per-session 绑定、unlimited 模式、路径解析与越界检测。 |
| `rollback.py` | `WorkspaceRollbackManager` 基于 SHA-256 对象存储 + SQLite 元数据，两阶段提交回退。 |

### 3.9 `claw/approval/` — 审批管理

| 文件 | 职责 |
|------|------|
| `manager.py` | `ApprovalManager`：线程安全内存审批存储，`threading.Event` 阻塞等待，10 分钟保留 + 200 条上限。 |

### 3.10 `claw/skills/` — Skill 系统

| 文件 | 职责 |
|------|------|
| `registry.py` | `SkillRegistry`：扫描本地 `skills/` 目录、解析 frontmatter、热重载、使用遥测。 |
| `management.py` | `validate_skill_package_bytes` / `install_skill_package_bytes` / `remove_skill_completely`：包校验与安装。 |
| `usage.py` | `SkillUsageStore`：sidecar `.usage.json` 存储使用统计与生命周期状态。 |

### 3.11 `claw/pet/` — 桌面宠物

| 文件 | 职责 |
|------|------|
| `app.py` | Tk 窗口主程序：精灵图集动画、气泡、拖拽、双击输入框、HTTP 轮询 Gateway。 |
| `catalog.py` | `PetCatalog`：宠物元数据/资源/设置持久化，ZIP 包校验与安装。 |
| `process.py` | `PetProcessManager`：子进程生命周期管理。 |
| `state.py` | `PetStateBroker`：Agent 事件 → 桌宠状态的线程安全投影。 |
| `replies.py` | `PetReplyStore` + `generate_and_store_pet_replies`：LLM 生成 12 条角色台词。 |

### 3.12 `claw/pi/` — Pi Agent 集成

| 文件 | 职责 |
|------|------|
| `client.py` | ~1000 行：`PiAgentClient`（JSONL RPC 子进程桥接）+ `RuntimeAgentClient`（按 session 路由）。 |
| `__init__.py` | 模块导出。 |
| `permission_gate.ts` | TS 扩展：路由 Pi 的 bash/edit/write 经过 SJTUClaw 审批 UI。 |
| `sjtuclaw_provider.ts` | TS 扩展：把 SJTUClaw 的 OpenAI 兼容配置注册为 Pi provider。 |
| `sjtuclaw_tools.ts` | TS 扩展：把 Python ToolRegistry 暴露给 Pi。 |

### 3.13 `claw/cli/` — 命令行接口

| 文件 | 职责 |
|------|------|
| `main.py` | `sjtuclaw` CLI 入口：`setup` / `gateway` / `chat` 子命令；setup 以合并写入方式配置 LLM、联网/时区、Gateway、可选 Pi 参数、高级参数和 QQ。 |
| `repl.py` | `run_repl`：交互式多轮对话主循环，注入所有依赖。 |
| `commands.py` | 斜杠命令识别与分发，`RuntimeState` dataclass 贯穿全局。 |

### 3.14 `claw/channels/` — 外部渠道

| 文件 | 职责 |
|------|------|
| `base.py` | `BaseChannel` 抽象基类 + `OutboundMessage`。 |
| `qq.py` | `QQChannel`：QQ Bot WebSocket + REST，token 刷新、重连、心跳。 |
| `qq_interactions.py` | 内联键盘构建与解析。 |
| `qq_constants.py` / `qq_crypto.py` / `qq_utils.py` / `qq_onboard.py` | 常量、加密、工具、扫码注册。 |

### 3.15 `claw/llm/` — LLM 客户端

| 文件 | 职责 |
|------|------|
| `client.py` | `LLMClient`：OpenAI 兼容客户端，指数退避重试，凭证脱敏。 |
| `protocol.py` | `AgentResponse` / `parse_agent_response`：原生 tool_calls 优先，JSON 协议回退。 |

### 3.16 `claw/prompts/` — Prompt 模板

| 文件 | 职责 |
|------|------|
| `templates.py` | 轻量模板引擎：`{{ var }}` 替换 + `{% if %}` 条件块。 |
| `__init__.py` | `load_system_prompt` / `load_soul` / `load_tool_contract` / `build_identity`。 |

### 3.17 配置与路径模块

| 文件 | 职责 |
|------|------|
| `config.py` | `LLMConfig` / `CompactionConfig` / `HeartbeatConfig` / `QQChannelConfig` + 路径常量。 |
| `runtime_settings.py` | Fernet 加密的 WebUI 可写设置（密钥不写回 .env）。 |
| `paths.py` | 源码版 / PyInstaller 版 / 安装版路径切换。 |
| `utils.py` | `force_utf8_stdio` / `now_iso` / `default_timezone_name` / `decode_subprocess_output`。 |

---

## 四、关键类与函数说明

### 4.1 Agent Loop 核心

```python
# claw/agent/loop.py
def run_agent_turn(
    session_id: str,
    user_message: str,
    *,
    rollback_manager=None,
    **kwargs,
) -> str:
    """Run a turn, capturing a workspace checkpoint before user input."""
    if rollback_manager is None:
        return _run_agent_turn_unlocked(session_id, user_message, **kwargs)
    with rollback_manager.turn_guard(session_id):
        session = session_store.get(session_id)
        message_id = f"msg_{uuid.uuid4().hex}"
        checkpoint_id = rollback_manager.create_turn_checkpoint(...)
        return _run_agent_turn_unlocked(...)
```

**模块级安全常量**（[claw/agent/loop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py#L180-L205)）：

```python
_APPROVAL_REQUIRED_LEVELS = {"write", "shell"}
_SKILL_SELECT_LEVEL = "skill_select"
_MAX_AGENT_ITERATIONS = _positive_env_int("CLAW_MAX_AGENT_ITERATIONS", 15)
_MAX_TOOL_CALLS_PER_TURN = _positive_env_int("CLAW_MAX_TOOL_CALLS_PER_TURN", 20)
_MAX_IDENTICAL_TOOL_CALLS = 3   # 连续 3 次相同 tool+args+结果 → 停滞
_MAX_REJECTIONS_PER_OPERATION = 3  # 同操作被拒 3 次 → 强制终止
_MAX_METRIC_SESSIONS = 500
```

### 4.2 Session 数据模型

```python
# claw/session/models.py
@dataclass
class Message:
    role: str            # user/assistant/tool/system
    content: str
    message_id: str = ""        # msg_<uuid.hex>
    tool_calls: list[dict] = None
    tool_call_id: str = ""
    name: str = ""
    media: list[str] = None     # 图片路径
    _command: bool = False      # 斜杠命令标记
    injected_event: str = ""    # "cron_trigger" 等
    latency_ms: int = 0

@dataclass
class Session:
    session_id: str
    title: str
    messages: list[Message]
    summary: str = ""
    last_consolidated: int = 0   # 已归档到摘要的消息索引
    revision: int = 0            # 修订号（防 ABA）
    metadata: dict = field(default_factory=dict)
```

### 4.3 Tool 数据结构

```python
# claw/tools/base.py
@dataclass
class ToolResult:
    ok: bool
    content: str | None = None
    error: str | None = None
    # __post_init__ 校验：成功不能带 error，失败不能带 content

@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], ToolResult]
    safety_level: str = "read_only"   # read_only/write/shell/download/network/skill_select
    concurrency_safe: bool = False
    max_result_chars: int = 0
```

### 4.4 ContextBuilder 装配顺序

```python
# claw/context/builder.py
class ContextBuilder:
    def build_messages(self, session, tool_registry=None, ...) -> list[dict]:
        # 装配顺序（不可变）：
        # 1. identity → system_prompt → soul → tool_contract → bootstrap
        # 2. memory block（按 version 缓存）
        # 3. skill index（轻量索引，完整内容通过 use_skill 注入）
        # 4. session summary（若存在）
        # 5. recent session messages
        # 6. _merge_leading_system_messages：合并连续 system 为一条
        #    （SJTU Qwen/LiteLLM 路由要求 index 0 处只有一条 system）
```

### 4.5 Compaction 触发与执行

```python
# claw/context/compaction.py
KEEP_RECENT_MESSAGES_MIN = 4
MAX_MESSAGE_TOKENS = 2000
KEEP_RECENT_TOKENS = 1000
_MAX_CONSOLIDATION_ROUNDS = 5
_SAFETY_BUFFER = 1024
_SUMMARY_FAILURE_COOLDOWN_S = 600   # 失败后 10 分钟冷却

def needs_compaction(session, *, max_message_tokens=None, ...) -> bool:
    """两个独立触发器：消息 token 超阈值 或 上下文预算压力超 ratio。"""

def compact_session(session, llm_client, *, keep_recent_tokens=None, ...) -> CompactionResult:
    """不修改 session，只计算新摘要；调用方在成功后才 apply_compaction_result。"""

def apply_compaction_result(session, result):
    """不删除原始消息，只推进 session.last_consolidated 边界。"""
```

### 4.6 Workspace 边界与回退

```python
# claw/workspace/manager.py
class WorkspaceManager:
    def resolve(self, session_id: str, path_str: str, *, must_exist=False) -> Path:
        """拒绝绝对路径；resolve() 后检查 relative_to(ws) 防止 ../ 越界。"""
    
    def set_unlimited(self, session_id: str, unlimited: bool):
        """启用后绕过边界检查，返回文件系统根。"""

# claw/workspace/rollback.py
class WorkspaceRollbackManager:
    def rollback(self, session_id, target=None) -> dict:
        """两阶段提交：
        1. 创建安全检查点（PREPARED）
        2. 应用 manifest（FILES_APPLIED）
        3. 恢复 session（COMMITTED）
        4. 失败时从安全点补偿（COMPENSATING→COMPENSATED）
        """
```

### 4.7 Approval 流程

```python
# claw/approval/manager.py
class ApprovalManager:
    def create(self, session_id, tool_name, tool_args) -> ApprovalRequest:
        """创建挂起审批。"""
    
    def wait(self, approval_id, timeout=300.0) -> ApprovalRequest | None:
        """阻塞等待决定，超时自动拒绝。"""
    
    # 清理策略：_COMPLETED_RETENTION_S=600（10分钟）+ _MAX_COMPLETED_KEEP=200
```

### 4.8 Cron 任务类型

```python
# claw/scheduler/types.py
@dataclass
class CronSchedule:
    kind: Literal["at", "every", "cron"]
    at_ms: int = 0          # 一次性绝对时间
    every_ms: int = 0       # 周期间隔
    expr: str = ""          # cron 表达式
    tz: str = ""            # IANA 时区

@dataclass
class CronJob:
    id: str
    name: str
    schedule: CronSchedule
    payload: CronPayload
    state: CronJobState
    repeat_times: int | None = None  # None=永久, N=运行 N 次后自动删除
```

---

## 五、依赖关系

### 5.1 Python 依赖（pyproject.toml）

```toml
dependencies = [
    "openai>=1.0.0",         # LLM API 客户端
    "python-dotenv>=1.0.0",  # .env 加载
    "fastapi>=0.100.0",      # Gateway HTTP 框架
    "uvicorn>=0.20.0",       # ASGI 服务器
    "python-multipart>=0.0.5",  # 文件上传
    "tiktoken>=0.5.0",       # token 计数
    "filelock>=3.0.0",       # 跨进程文件锁
    "Pillow>=10.0.0",        # 宠物图片处理
    "PyYAML>=6.0",           # SKILL.md frontmatter
    "croniter>=2.0",         # cron 表达式解析
    "aiohttp>=3.8.0",        # QQ Bot WebSocket
    "httpx>=0.25.0",         # 异步 HTTP 客户端
    "qrcode>=7.0",           # QQ 扫码注册
    "cryptography>=41.0",    # runtime_settings 加密
    "tzlocal>=5.0",          # 本地时区识别
]

[project.optional-dependencies]
desktop = ["pywebview>=5.0"]
build = ["pyinstaller>=6.0", "pywebview>=5.0"]
```

### 5.2 前端依赖（webui/package.json）

主要依赖：React 18、react-markdown、react-syntax-highlighter、rehype-katex、remark-gfm、Radix UI、Tailwind CSS、lucide-react。

### 5.3 跨模块依赖图

```text
gateway/server.py
  ├─ config.py (LLMConfig, paths, load_*_config)
  │    └─ paths.py (resource_root, data_dir, ...)
  │    └─ runtime_settings.py (setting_value)
  ├─ gateway/middleware.py (4 个中间件)
  ├─ scheduler/service.py (CronService)
  │    └─ scheduler/types.py (CronJob)
  ├─ scheduler/dispatcher.py (create_cron_dispatcher)
  │    └─ agent/loop.py (run_agent_turn)  ← 核心入口
  ├─ workspace/manager.py (WorkspaceManager)
  ├─ workspace/rollback.py (WorkspaceRollbackManager)
  ├─ approval/manager.py (ApprovalManager)
  ├─ skills/registry.py (SkillRegistry)
  │    └─ skills/usage.py (SkillUsageStore)
  ├─ context/builder.py (ContextBuilder)
  ├─ context/compaction_worker.py (CompactionWorker)
  ├─ memory/reflection.py (ReflectionManager)
  └─ runtime_settings.py (加密设置)
```

### 5.4 外部运行时依赖

- **Pi Agent**（可选）：同级目录的 `pi` 仓库或系统 `pi`/`pi.cmd`。通过 `PI_COMMAND`/`PI_CLI_PATH`/`PI_NODE_PATH` 等环境变量配置。
- **OpenAI 兼容模型服务**：OpenAI / Ollama / vLLM / LM Studio 等。
- **Inno Setup 7**（仅打包时）：生成 Windows 安装包。

---

## 六、项目运行方式

### 6.1 源码运行

```bash
# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# 2. 安装依赖
python -m pip install -r requirements.txt
python -m pip install -e .

# 3. 配置（交互式向导）
sjtuclaw setup
# 或手动复制 .env.example 为 .env 填写

# 4. 启动
sjtuclaw chat       # CLI 交互对话
sjtuclaw gateway    # Gateway + Web UI + REST API（默认 http://127.0.0.1:8000）
sjtuclaw-desktop    # Desktop：本地 Gateway + pywebview 独立窗口
```

### 6.2 前端开发

```bash
cd webui
npm install
npm run dev         # http://127.0.0.1:5173（Vite 热更新）
npx vitest run      # 测试
npm run build       # 输出到项目根目录 web/
```

### 6.3 Windows 安装包构建

```powershell
.\packaging\windows\build.ps1
# 产物：dist\SJTUClaw\SJTUClaw.exe + dist\installer\SJTUClaw-Setup-<version>.exe
```

### 6.4 三种运行模式对比

| 模式 | 启动命令 | 数据目录 | 适用场景 |
|------|----------|----------|----------|
| 源码 | `sjtuclaw gateway` | `<项目根>/data/` | 开发调试 |
| 桌面打包 | `SJTUClaw.exe` | `%USERPROFILE%\.sjtuclaw\data` | 终端用户 |
| 安装版 | 开始菜单 / 桌面快捷方式 | `%USERPROFILE%\.sjtuclaw\data` | 终端用户 |

### 6.5 关键环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `LLM_API_KEY` | 必填 | OpenAI 兼容 API Key |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | API 基础地址 |
| `LLM_MODEL` | 必填 | 模型名 |
| `LLM_CONTEXT_WINDOW` | 32000 | 上下文窗口 token 数 |
| `CLAW_MAX_AGENT_ITERATIONS` | 15 | 单 turn 最大迭代次数 |
| `CLAW_MAX_TOOL_CALLS_PER_TURN` | 20 | 单 turn 最大工具调用数 |
| `COMPACT_MAX_MESSAGE_TOKENS` | 2000 | 触发压缩的消息 token 阈值 |
| `HEARTBEAT_ENABLED` | true | 启用心跳监控 |
| `HEARTBEAT_INTERVAL_S` | 1800 | 心跳间隔（秒） |
| `GATEWAY_HOST` | 127.0.0.1 | Gateway 监听地址 |
| `GATEWAY_PORT` | 8000 | Gateway 端口 |
| `GATEWAY_API_TOKEN` | - | 非本机监听时必填 |
| `AGENT_BACKEND` | sjtuclaw | 默认后端（sjtuclaw/pi） |
| `QQ_ENABLED` | false | 启用 QQ Bot |

---

## 七、SJTUClaw.md 功能要求实现说明

本节按 `SJTUClaw.md` 中的 Step 0–Step 9 顺序，给出每个功能在本项目中的具体实现位置与关键代码。

### 7.1 多 Session 管理与持久化（Step 2）

**对应 Step 0（基础 LLM 调用）+ Step 1（多轮对话 Loop）+ Step 2（多 Session 与持久化）。**

#### 7.1.1 基础 LLM 调用（Step 0）

- **配置加载**：[claw/config.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/config.py) 的 `load_config()` 从 `.env` 或环境变量读取 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`，缺失时抛 `ConfigError` 并打印中文友好提示。
- **API KEY 安全**：`.gitignore` 包含 `.env`；`runtime_settings.py` 用 Fernet 加密 WebUI 编辑的密钥，密钥文件 `data/settings/runtime_settings.key` 权限 0o600。
- **LLM 客户端**：[claw/llm/client.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/client.py) 的 `LLMClient` 封装 OpenAI SDK，超时 120s，指数退避重试（`_MAX_RETRIES` 默认 2），`_scrub_secrets` 把 `sk-...`/`Bearer ...` 替换为 `***REDACTED***` 防泄露。
- **错误分类**：`LLMError` → `LLMConnectionError` / `LLMResponseStatusError` / `LLMResponseFormatError`，仅瞬态错误（连接/超时/429/5xx）才重试。

```python
# claw/llm/client.py
class LLMClient:
    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=_REQUEST_TIMEOUT,
        )

    def chat(self, messages, budget=None) -> str:
        response = self._call_api(messages, budget=budget)
        return self._extract_reply_text(response)
```

#### 7.1.2 多轮对话 Loop（Step 1）

- **入口**：[claw/cli/repl.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/repl.py) 的 `run_repl()` 持续读取用户输入，识别 `/exit` 退出命令，普通消息调用 `run_agent_turn()`。
- **历史维护**：每轮把 user 输入和 assistant 回复追加到当前 session；下次请求时由 `ContextBuilder.build_messages()` 发送 summary 与当前未压缩窗口，原始历史仍完整保存在 Session 中。
- **职责分离**：CLI 只负责 IO，LLM 调用逻辑全部在 `agent/loop.py`。

```python
# claw/cli/repl.py（简化）
def run_repl(client, session_store, memory_store, context_builder, tool_registry, ...):
    while True:
        user_input = _read_user_input()
        if user_input in EXIT_COMMANDS:
            break
        if is_command(user_input):
            handle_command(user_input, state)
            continue
        _handle_chat_turn(user_input, state, client, context_builder, tool_registry)
```

#### 7.1.3 多 Session 管理（Step 2）

**数据结构**：[claw/session/models.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/models.py)

```python
@dataclass
class Session:
    session_id: str           # 唯一标识
    title: str                # 用户可识别的标题
    messages: list[Message]   # 独立历史
    summary: str = ""         # 压缩摘要
    last_consolidated: int = 0
    revision: int = 0         # 修订号（防 ABA）
    metadata: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
```

**持久化**：[claw/session/store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) 的 `SessionStore` 采用 JSONL 格式——每个 session 一个文件，首行 metadata，后续每行一条消息。

```python
# claw/session/store.py
class SessionStore:
    def __init__(self, sessions_dir: Path):
        self._dir = sessions_dir
        self._lock = threading.RLock()           # 进程内锁
        # 文件名: _encode_key(session_id) → base64 URL-safe
    
    def save(self, session: Session, *, fsync: bool = False) -> None:
        # 原子写入：.tmp → os.replace()
        # Windows 上对 EACCES/EBUSY 瞬时错误重试（_REPLACE_RETRY_DELAYS）
    
    def fork_session_before_user_index(self, source_key, target_key, before_user_index):
        """会话分叉（回退时使用）"""
```

**命令支持**（[claw/cli/commands.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/commands.py)）：

```text
/session new              创建新 session 并切换
/session list             列出所有 session（含 sessionId/title/messages 数/updated）
/session switch <id>      切换到指定 session
/session rename <id> <title>  修改标题
/session delete <id>      删除 session
```

**自动标题**：[claw/session/title.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/title.py) 的 `auto_title_if_first_turn` 在会话首轮后调用 LLM 生成 ≤15 字中文标题，用户手动改名后设置 `metadata.title_user_edited=True` 阻止再次自动生成。

### 7.2 Agent Loop（Step 1、5、8）

**核心文件**：[claw/agent/loop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)

`run_agent_turn()` 是整个系统**主对话 turn 的统一入口**。CLI、Gateway、Scheduler、QQ Bot、Heartbeat 都必须路由到这里；Compaction、Reflection、自动标题等辅助任务可以调用各自的辅助 LLM 路径，但不能伪造主对话 turn。

#### 7.2.1 Think-Act-Observe 循环

```python
# claw/agent/loop.py（核心循环简化）
while True:
    if cancel_event.is_set():
        return _finish_reply(None, status="cancelled")
    if turn_count > _MAX_AGENT_ITERATIONS:
        return _finish_reply(..., status="max_iterations")
    
    messages = context_builder.build_messages(session, tool_registry, ...)
    response = llm_client.chat_with_tools(messages, tool_defs)
    
    if response.is_final:
        return _finish_reply(response.final)
    
    if response.is_tool_call:
        for tc in response.tool_calls:
            if tc.name == "use_skill":
                # skill_select: 创建 approval → 注入 skill 内容
                _handle_skill_select(tc.args, ...)
            elif tc.safety_level in {"write", "shell"}:
                # 写/Shell: 创建 approval → 等待用户决定
                force_approval = unlimited_mode or tc.safety_level == "shell"
                if auto_mode and not force_approval:
                    pass  # 仅 workspace 内的结构化 write 自动放行
                else:
                    req = _make_approval_request(...)
                    decided = approval_handler(req)
                    if decided.status != APPROVED:
                        # 记录拒绝，继续循环
                        continue
            # 执行工具，结果写入 session
            result = tool_registry.execute_by_name(tc.name, tc.args)
            session.messages.append(Message(role="tool", content=result.content, ...))
        continue
```

#### 7.2.2 安全限制

- 单 turn 最大迭代 15 次（`CLAW_MAX_AGENT_ITERATIONS`）
- 单 turn 最大工具调用 20 次（`CLAW_MAX_TOOL_CALLS_PER_TURN`）
- 连续 3 次相同 tool+args+结果 → 判定停滞，强制终止
- 同操作被拒 3 次 → 强制终止（避免 LLM 死循环重试）
- call_id 去重：扫描历史 tool_calls 已用 id，重名追加 `_2/_3` 后缀

#### 7.2.3 Pi 后端短路

如果 `llm_client` 暴露了完整 turn 方法，则先把 turn 委托给它。生产运行时使用 `RuntimeAgentClient` 按 Session 后端分流：Pi 交给 `PiAgentClient`，原生后端则以普通 `LLMClient` 重新进入 Think-Act-Observe 循环。

#### 7.2.4 事件流

通过 `event_callback` 发出 5 类事件，驱动 WebUI SSE 实时显示：

```python
# claw/agent/events.py
@dataclass
class ThinkingEvent(TurnEvent):      iteration: int = 0
@dataclass
class ToolCallStartEvent(TurnEvent): call_id: str; tool_name: str; args: dict; iteration: int
@dataclass
class ToolCallEndEvent(TurnEvent):   call_id: str; tool_name: str; ok: bool; result; error; duration_ms
@dataclass
class FinalEvent(TurnEvent):         content: str
@dataclass
class ErrorEvent(TurnEvent):         error: str
```

### 7.3 Context Build（Step 2、3、4、5）

**核心文件**：[claw/context/builder.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/builder.py)

`ContextBuilder` 是**原生主循环 messages 的统一装配模块**。CLI 和 Gateway 不自行构建主对话 messages；Pi 的 handoff/runtime prompt，以及 Compaction、Reflection、标题等辅助任务，使用各自的专用构建逻辑。

#### 7.3.1 装配顺序

```python
# claw/context/builder.py
class ContextBuilder:
    def build_messages(self, session, tool_registry=None, ...) -> list[dict]:
        # 1. 稳定前缀（按 workspace 缓存）
        prefix = self._get_stable_prefix(workspace_path, timezone, channel)
        #   identity → system_prompt → soul → tool_contract → bootstrap
        
        # 2. memory block（按 MemoryStore.version 缓存）
        memory_block = self._build_memory_block(memory_store)
        
        # 3. skill index（轻量索引，完整内容通过 use_skill 注入）
        skill_block = self._build_skill_block(skill_registry)
        
        # 4. session summary
        if session.summary:
            summary_block = f"## 会话摘要\n{session.summary}"
        
        # 5. recent session messages（从 last_consolidated 之后）
        recent = session.get_unconsolidated_messages()
        
        # 6. 合并连续 system 消息为一条（SJTU Qwen 路由要求）
        return self._merge_leading_system_messages(prefix + memory + skill + summary + recent)
```

#### 7.3.2 稳定上下文边界

- **system prompt**：从 `prompts/system_prompt.md` 加载，描述行为边界。
- **soul**：从 `prompts/soul.md` 加载，描述 claw 的稳定身份与风格。
- **memory**：跨 session 长期记忆，由 `MemoryStore` 管理。
- **session messages**：当前会话历史。

这四类上下文生命周期不同：system prompt/soul 由开发者维护，memory 通过命令管理，session messages 随对话增长。**system prompt、soul、memory 不参与 compaction**。

#### 7.3.3 Pi 上下文附加

`build_pi_append_prompt(session_id)` 为 Pi 后端构造可附加到其原生 prompt 的 SJTUClaw 上下文（memory + skill index），但**不**复制 SJTUClaw 工具契约（因为 Pi 用 `read`/`bash`/`edit`/`write`）。

#### 7.3.4 多模态支持

`_multimodal_user_content(text, media)` 把本地图片路径 base64 编码成 OpenAI `image_url` 格式，支持视觉模型。

### 7.4 Compaction 上下文压缩（Step 4）

**核心文件**：[claw/context/compaction.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction.py) + [claw/context/compaction_worker.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction_worker.py)

#### 7.4.1 触发策略

```python
# claw/context/compaction.py
KEEP_RECENT_MESSAGES_MIN = 4      # 绝对下限
MAX_MESSAGE_TOKENS = 2000         # 触发阈值
KEEP_RECENT_TOKENS = 1000         # 保留窗口
_MAX_CONSOLIDATION_ROUNDS = 5
_SAFETY_BUFFER = 1024
_SUMMARY_FAILURE_COOLDOWN_S = 600  # 失败后 10 分钟冷却

def needs_compaction(session, *, max_message_tokens=None, ...) -> bool:
    """两个独立触发器（任一满足即可）：
    1. 消息 token 超 MAX_MESSAGE_TOKENS
    2. 上下文预算压力超 context_usage_ratio
    都受 KEEP_RECENT_MESSAGES_MIN 下限保护。
    失败冷却：10 分钟内直接返回 False。
    """

def has_compactable_prefix(session, ...) -> bool:
    """确认保留窗口之外存在可安全归档的完整旧对话轮次。"""
```

运行时只保留两种触发方式：每轮完成后的 token 阈值自动压缩，以及用户显式执行
`/compact` 的手动压缩。会话空闲不会触发压缩。

#### 7.4.2 压缩流程

```python
def compact_session(session, llm_client, *, keep_recent_tokens=None, ...) -> CompactionResult:
    """不修改 session，只计算新摘要。"""
    # 1. 切分点算法：token budget → _find_split_index → _align_split_to_user_boundary
    #    （回退到最近的 user 消息边界，绝不切 mid-turn）
    # 2. 用 _COMPACTION_SYSTEM_INSTRUCTION 调用 LLM 生成摘要
    # 3. 工具输出预剪枝：超过 500 字符的 tool 消息替换为占位符
    # 4. _merge_summaries：旧摘要 + 新摘要用 "\n\n---\n\n" 连接

def apply_compaction_result(session, result):
    """不删除原始消息，只推进 session.last_consolidated 边界。"""
```

**关键原则**：compaction 失败时不能删除旧 messages。`CompactionError` 保证 session 完全未变。

#### 7.4.3 后台异步压缩

```python
# claw/context/compaction_worker.py
class CompactionWorker:
    def submit(self, session) -> bool:
        """同一时刻只允许一个压缩任务运行。"""
        with self._lock:
            if self._running: return False
            self._running = True
            # 取快照后释放锁，LLM 调用不阻塞主线程
            snapshot_messages = list(session.get_unconsolidated_messages())
            snapshot_revision = session.revision
        # daemon 线程执行 _do_compact
    
    def _do_compact(self, session, snapshot_messages, ...):
        # CompactionNotNeeded 是正常跳过，不重试、不输出失败信息
        # revision 守卫：防止 ABA 问题
        # 压缩期间用户回滚后发新消息，旧摘要对应的旧消息已不存在
        # 应用结果前再次加锁，检查 session.revision != snapshot_revision
        # 不一致则丢弃过期结果
```

#### 7.4.4 手动压缩命令

`/compact` 命令立即压缩当前 session，`force=True` 绕过 token 预算检查。

### 7.5 Memory 系统（Step 3）

**核心文件**：[claw/memory/store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/store.py)

#### 7.5.1 文件系统即数据库

每条记忆是 `data/memory/<category>/<slug>.md` 独立文件，YAML frontmatter 存结构化元数据，Markdown body 存富内容。人类可读、可编辑、可 git 追踪。

```python
# claw/memory/store.py
MEMORY_CATEGORIES = {"user_preference", "project", "decision", "fact", "general"}

@dataclass
class MemoryEntry:
    memory_id: str
    content: str          # Markdown body
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    importance: int = 3   # 1-5
    source_session_id: str = ""
    created_at, updated_at, last_recalled_at: str
    recall_count: int = 0
```

#### 7.5.2 多维度加权检索

```python
def recall(self, query, category=None, limit=5) -> list[MemoryEntry]:
    """打分算法：
    1. 标签匹配：完全包含 +10，词项匹配 +5
    2. 内容子串/词项匹配：完全子串 +8，每个词项 +3
    3. CJK 字符级匹配：当以上得分为 0 时启用，按匹配比例最高 +6
    4. 加成项（仅当有基础分）：
       - user_preference 类别 +2
       - importance 直接加分
       - 7 天内创建 +1
       - recall_count 频率加成（最高 +3）
       - 最近召回加成（1 小时内 +2，24 小时内 +1）
    召回副作用：更新 last_recalled_at 和 recall_count，并写回 .md 文件。
    """
```

#### 7.5.3 命令支持

```text
/memory add <content>                  添加记忆
/memory list                           列出所有记忆
/memory search <query>                 检索记忆
/memory update <memoryId> <content>    更新记忆内容
/memory delete <memoryId>              删除记忆
/memory status                         统计信息
```

#### 7.5.4 工具桥接

LLM 可通过 `remember`（write，需审批）和 `recall`（read_only，免审批）工具读写记忆：

```python
# claw/tools/memory_tools.py
def create_remember_tool(memory_store, session_id_provider):
    def handler(args):
        memory_store.add(
            content=args["content"],
            category=args.get("category", "general"),
            tags=args.get("tags", []),
            importance=args.get("importance", 3),
            source_session_id=session_id_provider(),
        )
        return ToolResult(ok=True, content=json.dumps({"saved": True}))
    return Tool(name="remember", safety_level="write", ...)
```

### 7.6 Tool 系统（Step 5、8）

**核心文件**：[claw/tools/base.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py)

#### 7.6.1 统一 Tool 数据结构

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict           # JSON Schema 子集
    handler: Callable[[dict], ToolResult]
    safety_level: str = "read_only"
    # read_only / write / shell / download / network / skill_select
    concurrency_safe: bool = False
    max_result_chars: int = 0

@dataclass
class ToolResult:
    ok: bool
    content: str | None = None
    error: str | None = None
```

#### 7.6.2 ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None:
        """名称正则校验 + schema 校验 + 冲突检测"""
    
    def execute_by_name(self, name, args, *, max_result_chars=0) -> ToolResult:
        """永不抛异常，失败封装为 ToolResult(ok=False, error=...)
        流程：prepare_call 钩子 → 参数校验 → handler 调用 → 类型检查 → 自动截断
        """
    
    def set_context(self, ctx: RequestContext) -> None:
        """传播给 ContextAware 工具（通过 ContextVar）"""
```

#### 7.6.3 只读 Tool（Step 5）

[claw/tools/readonly.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/readonly.py)：

- `current_time`：返回当前时间，可选 IANA 时区
- `list_dir`：列出目录内容，带文件大小
- `read_file`：读取文本文件，超过 64 KiB 截断

路径解析核心：

```python
def _resolve_path(path_str, workspace_manager, session_id_provider):
    """unlimited 模式：直接 resolve，跳过 workspace 检查
    workspace 已绑定：相对路径在 workspace 内解析，绝对路径必须在 workspace 内
    无 workspace：相对路径基于 main_dir() 解析
    """
```

#### 7.6.4 Update Tool（Step 8）

[claw/tools/update.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/update.py)：

- `create_file`：创建空文件，已存在则失败
- `overwrite_file`：覆盖写入（创建父目录），返回字符数
- `edit_file`：精确字符串替换，`old_string` 必须唯一匹配

统一通过 `_make_update_handler(operation, workspace_manager, session_id_provider)` 工厂生成 handler，内部用 `workspace_manager.resolve(session_id, path_str)` 解析路径。成功返回 JSON 字符串 `{"tool","path","result"}`。

#### 7.6.5 Shell Tool（Step 8）

[claw/tools/shell.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/shell.py)：

- `new_shell`：在 workspace 内启动 shell（Windows `cmd.exe` / POSIX `/bin/sh`）
- `run_command`：复用 shell 状态执行命令，cd 效果跨调用保留

**状态持久化机制**：通过临时文件记录 cwd。每次 `run_command` 从状态文件读取保存的 cwd → 构建平台原生包装脚本 → 解析真实 cwd 和 exit code → 写回状态文件 → 检查真实 cwd 是否在 workspace 内。

**多层边界防护**：
- `_precheck_directory_escape`：预扫描 `cd`/`chdir`/`pushd` 目标
- `_precheck_command_paths`：对 `del`/`rm`/`copy` 等路径感知命令的参数检测
- 执行后检查真实 cwd，越界则终止 shell

**Windows 命令翻译**：`rm`→`del /f /q`、`cp`→`copy`、`mv`→`move`、`cat`→`type`、`ls`→`dir`、`pwd`→`cd`、`clear`→`cls`、`touch`→`type nul >`。

#### 7.6.6 Download Tool（Step 8）

[claw/tools/download.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/download.py)：

```python
_downloads: dict[str, Path] = {}  # downloadId -> 绝对路径

def register_download(file_path: Path) -> str:
    """返回 dl_<uuid.hex[:12]>"""
    # 图片文件额外返回 inlineMarkdown 供前端内联展示
```

不返回文件内容给模型，只返回 `downloadId`。Gateway 通过 `/downloads/{id}` 端点提供文件下载。

#### 7.6.7 附件 Tool（Step 6 + 8）

[claw/tools/attachment.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/attachment.py) 的 `copy_attachment_to_workspace`：

```python
def handler(args):
    """只能访问当前 session 的附件。
    从 sessions_dir/<session_id>/attachments/.meta.json 读取附件元数据。
    若附件属于其他 session，返回明确错误提示跨 session 访问被禁止。
    用 shutil.copy2 保留元数据拷贝。
    """
```

#### 7.6.8 网络 Tool

[claw/tools/web.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/web.py) 的 `web_fetch` / `web_search`：

**SSRF 防护核心**：

```python
def _resolve_public_target(url, address_index=0):
    """拒绝非 http/https、含用户名密码、localhost/.local/.internal 主机
    DNS 预解析 + IP 钉扎：主机名解析后所有地址必须公网
    返回 IP 字面量连接 URL + Host 头 + SNI hostname，防止 DNS rebinding
    """
```

**搜索后端优先级**：Tavily（需 API key）→ DuckDuckGo（html + lite 双端点）→ Bing（RSS）。

#### 7.6.9 Tool Call 协议

[claw/llm/protocol.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/protocol.py) 支持两种协议：

1. **原生 function calling**（优先）：通过 OpenAI API 的 `tools` 参数传入。
2. **JSON 协议回退**：模型输出 JSON：

```json
{"type": "tool_call", "tool": "read_file", "args": {"path": "README.md"}}
{"type": "tool_calls", "calls": [{"tool": "current_time", "args": {}}, ...]}
{"type": "final", "content": "README.md 说明..."}
```

`parse_agent_response` 容忍 Markdown 代码围栏和外围文本，提取最外层 JSON。

### 7.7 Gateway 与 WebUI（Step 6）

#### 7.7.1 Gateway Server

**核心文件**：`claw/gateway/server.py`

模块级单例在 import 时初始化所有核心组件：

```python
_config, _llm_client = _load_initial_llm()
_session_store = SessionStore(SESSIONS_DIR)
_memory_store = MemoryStore(MEMORY_DIR)
_workspace_manager = WorkspaceManager()
_rollback_manager = WorkspaceRollbackManager(_workspace_manager, _session_store)
_context_builder = ContextBuilder(...)
_approval_manager = ApprovalManager()
_skill_registry = SkillRegistry()
_tool_registry = ToolRegistry()
_cron_service = CronService(_cron_store_path)
_compaction_worker = CompactionWorker(...)
_reflection_mgr = ReflectionManager(...)
```

**关键路由**：

| 路由 | 方法 | 说明 |
|------|------|------|
| `/chat` | POST | 同步对话，`asyncio.to_thread` 在后台线程运行 |
| `/chat/stream` | POST | SSE 流式对话，`queue.Queue` + 守护线程实时推送事件 |
| `/stop` | POST | 取消运行中的 turn |
| `/command` | POST | 执行 CLI 风格斜杠命令 |
| `/sessions` | GET/POST | 会话列表/创建 |
| `/sessions/{id}` | GET/DELETE | 会话详情/删除 |
| `/sessions/{id}/messages` | GET | 历史消息 |
| `/sessions/{id}/attachments` | GET/POST | 附件管理 |
| `/workspace` | POST/DELETE | 设置/取消工作区 |
| `/sessions/{id}/rollback` | POST | 回退操作 |
| `/approvals/{id}/approve\|reject` | POST | 审批操作 |
| `/skills` | GET | 技能列表 |
| `/skills/upload` | POST | 上传技能包 |
| `/cron/jobs` | GET/POST | 定时任务管理 |
| `/settings/llm` | GET/PUT | LLM 配置 |
| `/pet/state` | GET | 桌宠状态 |
| `/downloads/{id}` | GET | 文件下载 |

**并发控制**：通过 `_active_turns` 字典 + `_active_turns_lock` 实现 per-session 的并发控制（同一会话同时只能有一个 turn）。

**审批桥接**：

```python
def _gateway_approval_handler(req: ApprovalRequest) -> ApprovalRequest:
    """阻塞式审批处理器，通过 threading.Event 等待 REST 端点决定（300 秒超时）。"""
    event = threading.Event()
    _pending_approvals[req.approval_id] = (req, event)
    event.wait(timeout=300.0)
    return _pending_approvals.pop(req.approval_id, (req, None))[0]
```

#### 7.7.2 中间件层

[claw/gateway/middleware.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/middleware.py)：

- `GatewaySecurityMiddleware`：非回环客户端必须提供 `GATEWAY_API_TOKEN`，使用 `hmac.compare_digest` 防时序攻击。
- `RateLimitMiddleware`：滑动窗口 60 秒内最多 300 请求，允许 10 个并发突发。
- `RequestSizeMiddleware`：ASGI body 限制器，默认 10 MB，附件路径 50 MB。
- `RequestLoggingMiddleware`：结构化请求日志，慢请求（>5 秒）WARNING 级别。

#### 7.7.3 附件上传与 Session 隔离

附件存储结构：

```text
data/sessions/<sessionId>/attachments/
  <attachmentId-or-safe-file-name>
  .meta.json   # 附件元数据（filename, size, type, uploaded_at）
```

**Session 隔离**：`_attachments_dir` 拒绝含 `..`、`/`、`\\` 的 session_id。`copy_attachment_to_workspace` 工具只能访问当前 session 的附件。

#### 7.7.4 WebUI 前端

[webui/src/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/)：

- **技术栈**：React 18 + TypeScript + Vite + Tailwind CSS + Radix UI
- **渲染**：`react-markdown` + `react-syntax-highlighter`（dark: oneDark / light: oneLight）+ `rehype-katex` + `remark-gfm`
- **核心组件**：
  - `ThreadViewport`：消息流展示
  - `ThreadComposer`：输入框
  - `ToolCallCard`：工具调用卡片
  - `Sidebar`：会话列表
  - `SettingsView`：设置界面
  - `PetSprite`：桌宠精灵
  - `ErrorBoundary`：全局 + 局部错误边界

**SSE 实时反馈**：通过 `EventSource` 接收 `/chat/stream` 事件，实时展示 ThinkingEvent、ToolCallStartEvent、ToolCallEndEvent、FinalEvent。

### 7.8 Cron 定时任务系统（Step 7）

**核心文件**：`claw/scheduler/service.py`

#### 7.8.1 任务类型

```python
# claw/scheduler/types.py
@dataclass
class CronSchedule:
    kind: Literal["at", "every", "cron"]
    at_ms: int = 0          # 一次性绝对时间
    every_ms: int = 0       # 周期间隔
    expr: str = ""          # cron 表达式（croniter 解析）
    tz: str = ""            # IANA 时区

@dataclass
class CronJob:
    id: str
    name: str
    schedule: CronSchedule
    payload: CronPayload    # message, session_key, origin_channel, ...
    state: CronJobState     # next_run_at_ms, last_status, run_history, ...
    repeat_times: int | None = None  # None=永久, N=运行 N 次后自动删除
```

#### 7.8.2 调度核心

```python
class CronService:
    def _compute_next_run(self, schedule, now_ms):
        """支持 at/every/cron 三种调度类型"""
    
    def _execute_job(self, job):
        """执行单个任务：
        1. 依赖注入：_build_dependency_context 读取 depends_on 任务的最新输出
        2. run_claim：one-shot 任务 at-most-once 语义（TTL 30 分钟）
        3. _advance_next_run：执行前预先推进下次运行时间（crash 安全）
        4. 调用 on_job 回调
        5. 输出持久化到 runs/<job_id>/<timestamp>.md（保留最近 50 个）
        """
    
    def add_job(self, name, schedule, message, ..., depends_on=None, repeat_times=None) -> CronJob
    def remove_job(self, job_id) -> "removed"|"protected"|"not_found"
    def pause_job(self, job_id, reason="") / resume_job(self, job_id)
    def trigger_job(self, job_id)  # 手动触发
```

#### 7.8.3 Agent Loop 接入

[claw/scheduler/dispatcher.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/dispatcher.py)：

```python
def create_cron_dispatcher(*, session_store, context_builder, tool_registry,
                          llm_client, set_turn_session_id, update_cron_context,
                          on_heartbeat=None, on_deliver=None, ...):
    async def dispatch(job) -> str | None:
        if job.name == "heartbeat" and on_heartbeat:
            return await on_heartbeat(job)
        
        # agent_turn 类型任务
        sid = _resolve_session_key(job)
        await on_turn_active(sid)
        
        async def _run_bound_turn():
            set_turn_session_id(sid)
            update_cron_context(sid, job.name, ...)
            return await asyncio.to_thread(
                run_agent_turn,
                sid,
                f"[定时任务: {job.name}]\n\n{job.payload.message}",
                input_event="cron_trigger",
                ...
            )
        
        reply = await asyncio.to_thread(_run_bound_turn)
        if reply and job.payload.origin_channel:
            await on_deliver(job.payload.origin_channel,
                           job.payload.origin_chat_id, reply, ...)
        return reply
    
    return dispatch
```

**关键设计**：定时任务到期后调用同一套 `run_agent_turn`，复用 context builder、memory、tool、compaction。任务结果写入对应 session 历史，后续对话可见。

#### 7.8.4 Heartbeat 心跳监控

[claw/scheduler/callbacks.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/callbacks.py)：

```python
class HeartbeatCallback:
    def __call__(self, job) -> str | None:
        """读取 workspace/HEARTBEAT.md，检查是否有 ## active tasks 区域的活动任务
        有则调用 run_agent_turn 处理
        回复 "All clear." 时返回 None
        """
```

通过 `make_heartbeat_system_job(heartbeat_cfg)` 注册为系统作业，`kind="every"`，间隔由 `HEARTBEAT_INTERVAL_S`（默认 1800 秒）控制。

#### 7.8.5 持久化与崩溃恢复

- 任务持久化到 `data/cron/jobs.json`，原子写入（tempfile + `os.replace` + `fsync`）。
- 损坏文件保留为 `.corrupt-<ts>` 备份，拒绝用空列表覆盖以避免数据丢失。
- 程序重启后，未完成任务、状态、重复规则、下一次触发时间都不丢失。
- `_advance_next_run` 在执行前预先推进下次运行时间，crash 后不会重复执行。

#### 7.8.6 图形化入口

WebUI 提供：
- 创建一次性/周期性定时任务
- 查看任务列表与状态
- 查看执行历史
- 取消任务

### 7.9 Workspace（Step 8）

**核心文件**：[claw/workspace/manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/manager.py)

#### 7.9.1 Workspace 绑定

```python
class WorkspaceManager:
    def __init__(self):
        self._bindings: dict[str, Path] = {}  # session_id -> workspace_path
        self._unlimited: set[str] = set()
        # 持久化到 data/workspace/bindings.json
    
    def set(self, session_id: str, path_str: str) -> Path:
        """绑定并持久化，立即 resolve() 锚定 cwd"""
    
    def resolve(self, session_id: str, path_str: str, *, must_exist=False) -> Path:
        """拒绝绝对路径；resolve() 后检查 relative_to(ws) 防止 ../ 越界
        unlimited 模式返回文件系统根（Windows 系统盘 / Unix /）
        """
    
    def require(self, session_id: str) -> Path:
        """未绑定时抛 WorkspaceError"""
```

**命令支持**：

```text
/workspace set <path>     设置当前 session 的 workspace
/workspace show           查看当前 workspace
/workspace unset          取消 workspace 绑定
```

#### 7.9.2 边界强制

- 文件读取、修改、shell 命令、下载入口创建默认都在 workspace 内
- 相对路径按 workspace 解析
- 不允许通过 `../` 或绝对路径绕过边界
- `copy_attachment_to_workspace` 只能访问当前 session 绑定的附件

#### 7.9.3 Approval 流程

[claw/approval/manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py)：

```python
class ApprovalManager:
    def create(self, session_id, tool_name, tool_args) -> ApprovalRequest:
        """创建挂起审批，approval_id = apr_<uuid12>"""
    
    def approve(self, approval_id) -> ApprovalRequest | None:
        """批准并唤醒等待者"""
    
    def reject(self, approval_id, reason=""):
        """拒绝并唤醒"""
    
    def wait(self, approval_id, timeout=300.0) -> ApprovalRequest | None:
        """阻塞等待决定，超时自动拒绝"""
```

**Agent Loop 中的审批门**（[claw/agent/loop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)）：

- write/shell 工具：必须创建 approval，用户批准后才执行
- download/read_only/network：直接执行
- skill_select：创建 skill approval，用户确认后才注入 skill 内容
- **fail-closed**：`approval_handler is None` 且工具是 write/shell 时，永远拒绝

**Approval 拒绝后的处理**：

```python
# 拒绝结果也进入 session 历史，让模型知道操作未执行
rejection_key = f"{tc.name}:{json.dumps(tc.args, sort_keys=True)}"
rejection_count = _rejection_tracker.get(rejection_key, 0) + 1
_rejection_tracker[rejection_key] = rejection_count

if rejection_count >= _MAX_REJECTIONS_PER_OPERATION:
    # 强制终止，避免 LLM 死循环重试
    return _finish_reply(..., status="rejection_limit")
```

### 7.10 Skill 系统（Step 9）

**核心文件**：[claw/skills/registry.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/registry.py)

#### 7.10.1 Skill 数据结构

每个 skill 是 `skills/<name>/` 目录，至少包含 `SKILL.md`：

```markdown
---
name: course-report
description: 生成课程报告类 Markdown 草稿
---

# Course Report Skill

具体工作流程、输出要求、注意事项...
```

可选子目录：`assets/`、`references/`、`templates/`。

#### 7.10.2 SkillRegistry

```python
class SkillRegistry:
    def __init__(self, skills_dir=None, disabled_skills=None):
        """扫描本地 skills/ 目录，解析每个 SKILL.md 的 frontmatter"""
    
    def list_index(self, filter_unavailable=True) -> list[dict]:
        """轻量索引：[{name, description, ...}]
        用于注入 LLM context，让模型判断是否需要使用 skill
        """
    
    def format_full_content(self, name) -> str:
        """加载完整 SKILL.md + assets + references 列表"""
    
    def rescan(self, force=False) -> dict:
        """v7 热重载：通过 per-skill SKILL.md mtime 跟踪文件变化
        返回 {"added": [...], "removed": [...], "modified": [...]}
        """
```

#### 7.10.3 Skill 调用方式

**用户显式调用**：

```text
/skill course-report 帮我写一份 2000 字的思政课读书报告草稿...
```

`/skill` 命令返回 `__SKILL_INVOKE__|{name}|{task}` 哨兵，REPL 检测后启动带 skill 的 agent 回合。

**模型自主选择**：

1. `ContextBuilder._build_skill_block` 把轻量 skill 索引放入 LLM 上下文
2. 模型判断是否需要使用 skill
3. 模型通过 `use_skill` 工具（safety_level=skill_select）表达意图
4. Runtime 创建 skill approval，用户确认后注入完整 skill 内容
5. Agent loop 带着 skill 内容继续完成任务

```python
# claw/agent/loop.py
def _handle_skill_select(args, session, ..., skill_registry) -> tuple[str, str | None]:
    """1. 验证 skill 存在
    2. 调用 approval_handler 等待用户确认
    3. 注入 skill 内容到上下文
    返回 (工具结果 JSON, 可选的注入消息)
    """
```

#### 7.10.4 Skill 使用记录

每次 skill 调用都记录到 session：

```python
@dataclass
class SkillUsageRecord:
    skill_name: str
    session_id: str
    user_task: str
    source: str           # "explicit" 或 "auto"
    auto_reason: str = "" # auto 模式时模型为什么选择该 skill
    used_at: str
    output_path: str = ""
```

#### 7.10.5 Skill 管理

[claw/skills/management.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/management.py)：

- `validate_skill_package_bytes`：校验 zip/tar 包（文件类型、大小、数量、路径遍历、符号链接）
- `install_skill_package_bytes`：原子安装到 `skills/` 目录
- `remove_skill_completely`：删除技能目录及使用数据

[claw/tools/skill_manager_tool.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/skill_manager_tool.py) 的 `skill_manage` 工具允许 LLM 创建/编辑/删除 skill（需审批）。

#### 7.10.6 渐进式披露

[claw/tools/skills_tool.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/skills_tool.py)：

- `skills_list`：列出所有 skill 的 name + description（轻量）
- `skill_view`：加载 skill 完整 SKILL.md 或子文件，返回 `linked_files` 索引

**设计哲学**：无算法匹配、无需审批——LLM 像读普通文件一样读 skill 文档，自行判断相关性。

#### 7.10.7 内置 Skill

项目内置 3 个 skill（[skills/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/skills/)）：

- `course-report`：生成课程报告类 Markdown 草稿（必需）
- `material-summary`：汇总学习材料、课堂笔记
- `presentation-outline`：生成课堂展示大纲、PPT 页面结构

### 7.11 复用同一套 Runtime（Step 0-9 总要求）

SJTUClaw 严格遵守"复用同一套 runtime"原则：

| 入口 | 调用方式 | 是否复用 runtime |
|------|----------|------------------|
| CLI | `run_repl` → `run_agent_turn` | ✅ |
| Gateway | `POST /chat` → `asyncio.to_thread(run_agent_turn)` | ✅ |
| Scheduler | `dispatch` → `asyncio.to_thread(run_agent_turn)` | ✅ |
| QQ Bot | `_qq_message_handler` → `run_agent_turn` | ✅ |
| Heartbeat | `HeartbeatCallback.__call__` → `run_agent_turn` | ✅ |
| Cron Tool | `CronService._execute_job` → `dispatch` → `run_agent_turn` | ✅ |

**Gateway 只能调用已有 agent loop，不能单独调用底层 LLM client。** 所有入口共享：

- `SessionStore`：会话存储
- `ContextBuilder`：上下文装配
- `MemoryStore`：长期记忆
- `ToolRegistry`：工具注册表
- `CompactionWorker`：上下文压缩
- `ApprovalManager`：审批管理
- `WorkspaceManager`：工作区管理
- `SkillRegistry`：技能注册表

---

## 八、开发者新增功能实现说明

本节重点说明开发者在 SJTUClaw.md 基础要求之上新增的功能：Pet、Rollback、Reflect、Auto/Unlimited 模式、Pi Agent 接入。

### 8.1 Pet 桌面宠物

**模块**：[claw/pet/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/)

桌面宠物是 SJTUClaw 的特色功能，基于 Tkinter 实现独立窗口的精灵动画宠物，通过 HTTP 轮询 Gateway 获取 Agent 状态。

#### 8.1.1 架构

```text
┌─────────────────────────────────────────┐
│  Gateway (server.py)                    │
│  ├─ /pet/state 端点                     │
│  ├─ PetStateBroker（事件 → 状态投影）   │
│  └─ PetCatalog（资源管理）              │
└──────────────┬──────────────────────────┘
               │ HTTP 轮询（每秒）
               v
┌─────────────────────────────────────────┐
│  Pet 子进程 (claw/pet/app.py)           │
│  ├─ DesktopPet (Tk 窗口)                │
│  ├─ 精灵图集动画（9 种动画 + 16 方向）  │
│  ├─ 气泡显示（回复/审批/错误）          │
│  ├─ 拖拽 + 双击输入框                   │
│  └─ PetReplyStore（俏皮回复）           │
└─────────────────────────────────────────┘
```

#### 8.1.2 核心类

**`PetStateBroker`**（[claw/pet/state.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/state.py)）：Agent 事件到桌宠状态的线程安全投影。

```python
@dataclass
class _TaskState:
    session_id: str
    task: str                # 截断到 72 字符
    phase: str = "thinking"  # thinking/acting/waiting_approval/idle
    message: str = "正在思考"
    animation: str = "running"
    updated_at: str
    finished_at: str | None = None
    ttl: float = 8.0

class PetStateBroker:
    def handle_event(self, session_id, event):
        """按 type(event).__name__ 分派：
        ThinkingEvent → phase=thinking, animation=running
        ToolCallStartEvent → phase=acting, animation=对应工具
        ToolCallEndEvent → 更新 message
        ErrorEvent → animation=failed
        FinalEvent → finish_turn
        """
    
    def snapshot(self) -> dict:
        """清除过期任务，优先返回 waiting_approval 状态"""
```

**`DesktopPet`**（[claw/pet/app.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/app.py)）：Tk 窗口主程序。

```python
class DesktopPet:
    def __init__(self, gateway_url: str, data_dir: Path):
        # 加载宠物资源（PetCatalog）
        # 加载俏皮回复（PetReplyStore）
        # 创建 Tk 窗口 + Canvas
        # 启动后台轮询线程 _poll_gateway
    
    def _poll_gateway(self):
        """每秒拉取 /pet/state，通过 queue.Queue 推给主线程"""
    
    def _drain_updates(self):
        """主线程消费队列，更新动画/气泡（Tk 非线程安全）"""
```

#### 8.1.3 动画系统

```python
# claw/pet/app.py
ANIMATIONS: dict[str, tuple[int, list[int]]] = {
    "idle":           (0, [400, 400, 400, 400]),  # 行号 + 每帧时长
    "running-right":  (1, [80]*8),
    "running-left":   (2, [80]*8),
    "waving":         (3, [200]*4 + [400]*2),
    "jumping":        (4, [120]*6),
    "failed":         (5, [300]*4),
    "waiting":        (6, [600]*4),
    "running":        (7, [100]*8),
    "review":         (8, [500]*4),
}

CELL_WIDTH = 192
CELL_HEIGHT = 208
PET_BASE_SCALE = 121 / CELL_HEIGHT  # 与 Codex 浮窗逻辑像素对齐
```

**精灵图集版本**：
- v1：8 列 × 9 行，1536 × 1872，9 行基础动画
- v2：8 列 × 11 行，1536 × 2288，增加 2 行共 16 个观察方向（看鼠标）

**闲置看鼠标**：图集 ≥11 行时调用 `_show_look_frame`，根据鼠标相对宠物的角度选 16 方向中的 1 帧。

#### 8.1.4 交互

- **拖拽**：阈值 4 像素，根据 dx 正负切 `running-right`/`running-left` 连续动画；释放后异步 `save_position`。
- **单击**：延迟 400ms 显示俏皮回复（从 `PetReplyStore` 随机选一句）。
- **双击**：取消单击延迟，弹出输入框 `_open_input_popup`，支持粘贴剪贴板图片。
- **气泡**：本地消息 TTL 4 秒，回复消息 TTL 15 秒，上限 200 字符。

#### 8.1.5 俏皮回复生成

[claw/pet/replies.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/replies.py)：

```python
def generate_and_store_pet_replies(pet: dict, llm_client: Any, store: PetReplyStore) -> PetReplyGeneration:
    """LLM 根据 displayName 和 description 生成 12 条符合角色人设的回复
    每条 4-28 字符，只返回 JSON 字符串数组
    失败时回退到 fallback_pet_replies(display_name)
    永不阻断宠物导入
    """
```

回复按宠物 ID 独立保存到 `data/pet/replies/<pet-id>.json`，删除宠物时同步清理。

#### 8.1.6 宠物包安装

[claw/pet/catalog.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/catalog.py) 的 `install_package`：

ZIP 包校验：
- 路径遍历/符号链接/设备文件/加密/压缩比异常全部拒绝
- `pet.json` 字段校验（id 正则、displayName 长度、spriteVersionNumber 1 或 2）
- 图片真实格式校验（防伪扩展名）
- 透明通道校验
- 图集尺寸校验（v1: 1536×1872, v2: 1536×2288）
- 必用动画帧校验（已使用帧必须有内容，未使用帧必须完全透明）

#### 8.1.7 进程管理

[claw/pet/process.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/process.py) 的 `PetProcessManager`：

```python
class PetProcessManager:
    def start(self) -> bool:
        """frozen 模式: sys.executable --pet --gateway-url ... --data-dir ...
        源码模式: python -m claw.pet ...
        Windows 加 CREATE_NO_WINDOW
        """
    
    def stop(self, timeout=3.0) -> bool:
        """terminate() → wait(timeout) → 仍存活则 kill()"""
```

通过 `run_desktop_pet` 入口加单例文件锁（`msvcrt.locking`/`fcntl.flock`），防止多实例。

### 8.2 Rollback 工作区回退

**核心文件**：`claw/workspace/rollback.py`

为 Session 设置 workspace 后，系统自动在每次用户消息执行前创建检查点，支持原子性恢复 workspace 文件和对话状态。

#### 8.2.1 存储设计

**内容寻址对象存储**：

```python
def _store_blob(self, path: Path) -> str:
    """SHA-256 哈希 → objects/<前2字符>/<后62字符>
    天然去重，相同内容只存一份
    """
```

**SQLite 元数据库**：

| 表 | 用途 |
|----|------|
| `bindings` | session→workspace 绑定，含 `generation`（变更代数） |
| `checkpoints` | 检查点，含 `manifest_json`、`session_json`（zlib+base64 压缩）、`kind`、`status` |
| `operations` | 回退操作记录，含 `status`（PREPARED/FILES_APPLIED/COMMITTED/FAILED/COMPENSATED） |

**检查点类型**：
- `baseline`：启用回退时创建的基线
- `turn`：每次用户 turn 前创建
- `rollback_safety`：回退操作前的安全点
- `operation_safety`：操作中的安全点

#### 8.2.2 锁设计

```python
class WorkspaceRollbackManager:
    def __init__(self, ...):
        self._meta_lock = threading.RLock()        # 元数据锁
        self._storage_lock = threading.RLock()     # 对象存储锁
        self._workspace_locks = {}                  # 按 workspace 路径分桶
        self._session_locks = {}                    # 按 session 分桶
    
    @contextmanager
    def turn_guard(self, session_id):
        """turn_guard 先获取 session 锁再获取 workspace 锁
        防止 turn 中途切换根
        """
```

#### 8.2.3 两阶段提交回退

```python
def rollback(self, session_id, target=None) -> dict:
    """两阶段提交：
    1. 创建安全检查点（PREPARED）
    2. 应用 manifest（FILES_APPLIED）：
       _apply_manifest(root, wanted)
       - 先删除多余路径（深度优先）
       - 再创建目录
       - 最后恢复文件/符号链接
       - 每步校验哈希
    3. 恢复 session（COMMITTED）：从检查点的 session_json 反序列化
    4. 失败时从安全点补偿（COMPENSATING→COMPENSATED）
    """
```

#### 8.2.4 Agent Loop 集成

```python
# claw/agent/loop.py
def run_agent_turn(session_id, user_message, *, rollback_manager=None, **kwargs) -> str:
    if rollback_manager is None:
        return _run_agent_turn_unlocked(session_id, user_message, **kwargs)
    
    with rollback_manager.turn_guard(session_id):
        session = session_store.get(session_id)
        message_id = f"msg_{uuid.uuid4().hex}"
        checkpoint_id = rollback_manager.create_turn_checkpoint(
            session_id, session,
            message_id=message_id,
            message_preview=user_message,
            partial=bool(kwargs.get("unlimited_mode", False)),
        )
        return _run_agent_turn_unlocked(
            session_id, user_message,
            _rollback_message_id=message_id,
            _rollback_checkpoint_id=checkpoint_id,
            **kwargs,
        )
```

#### 8.2.5 命令支持

```text
/rollback                 回退一轮
/rollback 3               回退到倒数第 3 个用户回合之前
/rollback <checkpointId>  回退到指定检查点
/rollback list            列出可用检查点
/rollback status          查看状态
/rollback undo            撤销最近一次回退（单步，新 turn 后失效）
```

#### 8.2.6 关键特性

- **不删除原始消息**：compaction 只推进 `last_consolidated` 边界，原始 transcript 仍可用于回滚/审计。
- **revision 守卫**：后台 compaction 结果应用前检查 `session.revision`，不会覆盖回退后的状态。
- **不依赖 Git**：完全自建对象存储，不修改或依赖 workspace 中的 Git 仓库。
- **崩溃恢复**：`recover_incomplete_operations` 启动时补偿被进程退出中断的操作。
- **垃圾回收**：`garbage_collect` mark-and-sweep 未引用对象。
- **UNLIMITED 限制**：开启 UNLIMITED 后发生在 workspace 外的改动不会被恢复，预览和执行结果会明确提示。

### 8.3 Reflect 每日反思

**核心文件**：[claw/memory/reflection.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/reflection.py)

后台线程每分钟检查一次是否到达配置的每日时间，触发后将所有自上次运行以来修改过的会话交给 LLM 提取结构化记忆事实，自动保存到 `MemoryStore`（无需审批）。

#### 8.3.1 配置

```python
@dataclass
class ReflectionConfig:
    enabled: bool = True
    time: str = "23:00"          # 每日触发时间
    last_run_at: str = ""
    run_history: list[ReflectionRun] = field(default_factory=list)
```

配置持久化到 `data/memory/reflection_config.json`，原子写入。

#### 8.3.2 触发逻辑

```python
class ReflectionManager:
    def start(self):
        """启动后台守护线程，每 60 秒检查一次"""
    
    def _tick(self):
        """触发保护：
        - _ran_today 防止同一分钟内重复触发
        - _same_day 防止同一天重复运行
        - run_now 在 cron 上下文中禁止调度新任务
        """
```

#### 8.3.3 LLM 提取流程

```python
def _extract_facts_batch(self, sessions_data, existing_memories):
    """构建单一 user message：
    - 已有记忆块（避免重复提取）
    - 各会话的摘要 + 最近 20 条消息（每条截断 300 字符）
    
    系统提示词要求返回纯 JSON 数组：
    [{"category","content","tags","importance"}]
    
    _parse_facts_from_response 容忍 markdown 代码围栏和外围文本
    提取最外层 JSON 数组并校验字段
    """
```

系统提示词（[claw/memory/reflection.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/reflection.py#L39-L53)）：

```python
_REFLECTION_SYSTEM_PROMPT = (
    "你是一个记忆整理助手。你的任务是回顾用户最近的对话，"
    "从中提取值得长期记忆的关键信息。\n\n"
    "提取规则：\n"
    "1. 只提取有长期价值的信息（项目、偏好、决策、重要事实）。\n"
    "2. 忽略临时的、一次性的问题、调试细节、寒暄。\n"
    "3. 如果某条信息与已有记忆明显重复，不要重复提取。\n"
    "4. 每条记忆用简洁的一句话表达。\n\n"
    "返回格式（纯 JSON 数组，不要任何额外文字）：\n"
    '[\n'
    '  {"category":"project","content":"用户正在开发智能客服系统","tags":["fastapi","postgresql"],"importance":4},\n'
    '  {"category":"user_preference","content":"用户喜欢中文交流","tags":["language"],"importance":3}\n'
    ']'
)
```

#### 8.3.4 命令支持

```text
/reflect status    查看反思配置与上次运行状态
/reflect enable    启用每日反思
/reflect disable   禁用每日反思
/reflect time HH:MM  设置每日触发时间
/reflect now       立即触发一次反思
```

### 8.4 Auto 与 Unlimited 模式

**实现位置**：[claw/workspace/manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/manager.py) + [claw/agent/loop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)

AUTO 和 UNLIMITED 是两个相互独立、按 Session 生效的执行模式。新建 Session 时二者默认均为关闭状态，Gateway 重启后也会恢复为关闭状态。

#### 8.4.1 模式对比

| 模式 | 作用 | 审批行为 | 文件系统边界 |
|------|------|----------|--------------|
| 默认模式 | 使用完整安全保护 | 写入和 Shell 操作逐次审批 | 仅允许访问当前 workspace |
| AUTO | 减少 workspace 内操作的人工确认 | 自动批准结构化文件写入；Shell 和 Skill 加载确认仍保留 | 仍严格限制在当前 workspace，越界操作由工具拒绝 |
| UNLIMITED | 解除 workspace 路径限制 | 写入、覆盖、删除和 Shell 操作始终逐次审批，AUTO 无法跳过 | 可访问 workspace 外路径 |

#### 8.4.2 WorkspaceManager 中的实现

```python
# claw/workspace/manager.py
class WorkspaceManager:
    def set_unlimited(self, session_id: str, unlimited: bool):
        """启用后绕过边界检查，返回文件系统根
        Windows 为系统盘，Unix 为 /
        """
    
    def is_unlimited(self, session_id: str) -> bool:
        return session_id in self._unlimited
    
    def resolve(self, session_id: str, path_str: str, *, must_exist=False) -> Path:
        if self.is_unlimited(session_id):
            return Path(path_str).resolve()  # 跳过 workspace 检查
        ws = self.require(session_id)
        # 拒绝绝对路径；resolve() 后检查 relative_to(ws)
```

#### 8.4.3 Agent Loop 中的审批门

```python
# claw/agent/loop.py（核心逻辑）
if tc.safety_level in _APPROVAL_REQUIRED_LEVELS:  # {"write", "shell"}
    # UNLIMITED 模式强制审批（即使 AUTO 开启）
    # 因为相对路径和 shell 命令仍可能逸出 workspace
    force_approval = unlimited_mode
    
    if auto_mode and not force_approval:
        # AUTO 模式：跳过审批，工具在 workspace 内自由操作
        print(f"[auto] 自动批准 {tc.name}（AUTO 模式）")
    else:
        # 创建 approval → 等待用户决定
        req = _make_approval_request(session_id, tc.name, tc.args)
        decided = approval_handler(req)
        if decided.status != ApprovalStatus.APPROVED.value:
            # 记录拒绝，追踪连续拒绝次数
            rejection_key = f"{tc.name}:{json.dumps(tc.args, sort_keys=True)}"
            rejection_count = _rejection_tracker.get(rejection_key, 0) + 1
            _rejection_tracker[rejection_key] = rejection_count
            if rejection_count >= _MAX_REJECTIONS_PER_OPERATION:
                # 强制终止，避免 LLM 死循环重试
                return _finish_reply(..., status="rejection_limit")
            continue
```

#### 8.4.4 命令支持

```text
/auto on       开启 AUTO 模式
/auto off      关闭 AUTO 模式
/auto status   查看当前 Session 的 AUTO 状态

/unlimited on       允许访问 workspace 外路径
/unlimited off      恢复 workspace 边界
/unlimited status   查看当前 Session 的 UNLIMITED 状态
```

#### 8.4.5 关键安全设计

- **AUTO ≠ 取消安全边界**：它只省略 workspace 沙箱内结构化文件写入的逐次审批；Shell 命令始终需要明确审批。
- **UNLIMITED 才解除路径边界**，但不会取消危险操作审批。
- **两个模式同时开启时，UNLIMITED 的强制审批规则优先**。
- **Skill 加载确认不受 AUTO 影响**：`skill_select` 始终需要用户确认。
- **per-session 隔离**：模式状态绑定到 session，不影响其他 session。

### 8.5 Pi Agent 接入

**核心文件**：`claw/pi/client.py` + 3 个 TypeScript 扩展

SJTUClaw 通过官方 JSONL RPC 接入 Pi 编码 Agent，保留其模型提供商、工具循环、Skills、Extensions、自动压缩、重试和持久会话，同时沿用 SJTUClaw 的界面、渠道与审批体验。

#### 8.5.1 架构

```text
SJTUClaw 进程
├─ RuntimeAgentClient（按 session 路由）
│   ├─ session A: sjtuclaw 后端 → 自研 Agent Loop
│   └─ session B: pi 后端 → PiAgentClient
│                    │
│                    v
│              Pi 子进程（JSONL RPC）
│              ├─ stdin: SJTUClaw 发送的 JSON 命令
│              ├─ stdout: Pi 返回的 JSON 事件流
│              ├─ 加载 3 个 TS 扩展：
│              │   ├─ permission_gate.ts（审批路由）
│              │   ├─ sjtuclaw_provider.ts（LLM provider 注册）
│              │   └─ sjtuclaw_tools.ts（工具桥接）
│              └─ Pi 原生工具循环
```

#### 8.5.2 后端切换

```python
# claw/pi/client.py
def default_agent_backend() -> str:
    """读 AGENT_BACKEND 环境变量，默认 'sjtuclaw'"""

def get_session_backend(session_store, session_id, *, persist=True) -> str:
    """按 session 读取后端，元数据存储在 session.metadata['agent_backend']"""

def set_session_backend(session_store, session_id, backend) -> str:
    """切到 pi 时生成新的 pi_session_generation（16 字节 hex）
    用于构造 Pi session token
    """
```

`/pi on` / `/pi off` 命令独立切换当前 session 的后端，互不影响。标题栏会显示 Pi 状态徽标。

#### 8.5.3 PiAgentClient

```python
class PiAgentClient(LLMClient):
    def run_agent_turn(self, session_id, user_message, *, session_store, ...) -> str:
        """完整主 agent 回合委托给 Pi：
        1. _build_command(config, pi_session_id): 拼接 Pi 子进程命令行
           - 加载 3 个 TS 扩展
           - 追加 system prompt 文件
           - provider/model/thinking 参数
           - 所有 skill 目录
        2. _run_rpc(...): 启动子进程，JSONL 双向通信
           - 监听 response/extension_ui_request/agent_start/tool_execution_start/end
           - message_update/message_end/extension_error/agent_settled
           - 支持 cancel_event 发送 abort 消息
        3. _handle_ui_request: 处理 Pi 的 select/confirm/input/editor UI 请求
           - input + title="SJTUClaw 工具桥接" → _execute_host_tool
           - confirm + title="SJTUClaw 工具审批" → approval_handler
        4. _PiToolMessageRecorder: 把 Pi 工具事件翻译成 SJTUClaw 原生消息协议持久化
        """
    
    def compact_session(self, session_id, *, session_store) -> str:
        """跑 Pi 原生压缩"""
```

#### 8.5.4 TypeScript 扩展

**`permission_gate.ts`**（[claw/pi/permission_gate.ts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pi/permission_gate.ts)）：

```typescript
const readOnlyTools = new Set(["read", "ls", "find", "grep"]);
const guardedTools = new Set(["bash", "edit", "write"]);

export default function permissionGate(pi: ExtensionAPI) {
    pi.on("tool_call", async (event, ctx) => {
        if (readOnlyTools.has(event.toolName)) return undefined;
        if (!guardedTools.has(event.toolName)) return undefined;
        const confirmed = await ctx.ui.confirm(
            "SJTUClaw 工具审批",
            JSON.stringify({ toolName: event.toolName, input: event.input }),
        );
        if (!confirmed) return { block: true, reason: "SJTUClaw user approval was not granted" };
        return undefined;
    });
}
```

**`sjtuclaw_provider.ts`**：把 SJTUClaw 的 OpenAI 兼容配置注册为 Pi provider，支持复用已有 LLM 配置。

**`sjtuclaw_tools.ts`**：把 Python `ToolRegistry` 暴露给 Pi，通过 `SJTUCLAW_PI_TOOL_MANIFEST` 环境变量传递工具清单，`SJTUCLAW_PI_BRIDGE_TOKEN` 校验桥接请求。

#### 8.5.5 工具桥接

```python
def _execute_host_tool(self, name, args, bridge_token, ...):
    """用 hmac.compare_digest 校验 bridge_token
    按 tool.safety_level in {"write","shell","download"} 判断是否需要审批
    最终调用 tool_registry.execute_by_name(name, args, max_result_chars=50_000)
    """
```

#### 8.5.6 Session Handoff

`_handoff_prompt(summary, messages, current_prompt)` 把 SJTUClaw 历史 JSON 化（上限 50KB）作为 `<sjtuclaw_session_handoff>` 注入 Pi 新分支，保证上下文连续性。

#### 8.5.7 Windows 兼容

`_preferred_windows_bash()` 在 Windows 上找原生 Git Bash，避免 Pi 误用 WSL 的 `System32\bash.exe`。用 `_is_usable_native_bash` 探针（`uname -r` 含 microsoft 就退出 42）。

#### 8.5.8 配置

Pi 是可选外部依赖，不需要把完整 Pi SDK 或源码提交到本仓库。源码开发时推荐保持同级目录布局：

```text
SJTUClaw/
├── SJTUClaw/    # 本仓库
└── pi/          # 外部 Pi 仓库或构建产物
```

关键环境变量：

| 变量 | 说明 |
|------|------|
| `AGENT_BACKEND` | 默认后端（sjtuclaw/pi） |
| `PI_COMMAND` | Pi 启动命令 |
| `PI_CLI_PATH` | Pi CLI 路径 |
| `PI_NODE_PATH` | Node.js 路径 |
| `PI_PROVIDER` | Pi 的 LLM provider |
| `PI_MODEL` | Pi 使用的模型 |
| `PI_THINKING` | thinking 级别 |
| `PI_TRUST_TOOLS` | 是否信任 Pi 内置工具 |
| `PI_TURN_TIMEOUT_S` | 单 turn 超时（默认 1800） |

---

## 九、核心运行机制与模块关系

本章不再按目录逐个介绍文件，而是从“状态保存在哪里、一次请求如何流动、各模块如何互相约束”三个角度解释运行时。阅读本章时应先区分三类调用：

1. **主对话 turn**：用户消息、Cron、Heartbeat、QQ 等入口最终都进入 `claw.agent.loop.run_agent_turn()`，并由它统一处理回退检查点、会话写入、工具循环、审批、事件和最终回复。
2. **后端完整 turn**：`RuntimeAgentClient` 可以把主对话 turn 路由给 SJTUClaw 原生循环，也可以交给 Pi 自己完成完整工具循环。
3. **辅助 LLM 任务**：Compaction、Reflection、自动标题等不属于主对话 turn，可以通过 `chat()` 使用辅助 LLM；它们不能伪造用户 turn，也不能绕过各自的持久化和并发守卫。

### 9.1 状态归属与隔离总表

理解系统关系的关键不是“是否存在全局单例”，而是“单例内部的数据以什么键隔离”。Gateway 确实只创建一套 Store、Registry 和 Service，但其中大多数可变状态都以 `session_id` 为键。

| 状态 | 归属/隔离粒度 | 持久化位置 | 并发与安全边界 |
|------|---------------|------------|----------------|
| 对话消息、摘要、后端选择 | 每个 Session | `data/sessions/<encoded-id>.jsonl` | per-session 线程锁 + 文件锁 + 原子替换 |
| 当前活动 turn | 每个 Session | 仅内存 | 同一 Session 拒绝并发 turn，不同 Session 可并行 |
| 附件 | 每个 Session | `data/sessions/<session-id>/attachments/` | 解析时校验附件所属 Session |
| Workspace 绑定 | 每个 Session | `data/workspace/bindings.json` | 合并写入 + 文件锁；路径解析强制边界 |
| AUTO 状态 | 每个 Session | 仅内存 | Gateway/CLI 分别维护映射，重启后关闭 |
| UNLIMITED 状态 | 每个 Session | 仅内存 | `WorkspaceManager` 内集合，重启后关闭 |
| Pi/SJTUClaw 后端 | 每个 Session | Session metadata | 切换到 Pi 时旋转 generation |
| 回退检查点 | Session + Workspace generation | `data/workspace/rollback/` | Session 锁 + 规范化 workspace 根锁 |
| Cron 任务 | 每个 Job，payload 引用 Session | `data/cron/jobs.json` 及输出目录 | claim 先持久化，再执行回调 |
| Skill 定义 | Runtime 全局 | `skills/**/SKILL.md` | 热重载；使用记录另存 |
| Memory | Runtime 全局、跨 Session 共享 | `data/memory/<category>/<slug>.md` | 原子写；记录 `source_session_id` 来源 |
| ToolRegistry | Runtime 全局 | 不持久化 | 每次执行通过 ContextVar 绑定当前 turn |
| CompactionWorker | Runtime 全局单 worker | 结果写回 Session | Session revision 守卫拒绝过期摘要 |

因此，“Session 隔离”并不意味着所有信息都禁止跨 Session：对话、workspace、模式、附件和后端严格按 Session 隔离；Memory 和 Skill 则是有意设计成全局知识层。它们通过来源标记、显式检索和只读索引进入当前上下文，而不是把另一个 Session 的原始对话直接拼入当前 Session。

### 9.2 Session 隔离

#### 9.2.1 数据模型与磁盘布局

`claw/session/models.py` 中的 `Session` 是一个独立会话的状态根，主要包含：

- `session_id`、`title`、创建/更新时间；
- 完整的 `messages` 原始消息序列；
- `summary` 与 `last_consolidated` 压缩边界；
- `skill_usage`；
- 单调变化的 `revision`；
- `metadata`，用于保存 `agent_backend`、Pi generation、运行时检查点、待恢复用户 turn 等扩展状态。

`SessionStore` 不把所有会话塞进一个大文件，而是为每个 Session 保存一份 JSONL。第一行是 metadata，后续每行一条消息。这样有三个效果：

1. 一个 Session 的高频写入不会重写其他 Session；
2. 单条消息损坏时可以跳过该行，不必丢弃整个数据库；
3. 完全无法解析的文件可以被隔离为损坏备份，而不是静默覆盖。

保存流程使用“写临时文件 → flush/fsync → 原子 replace”。进程内有缓存锁和 per-session `RLock`，进程间还有同路径的 `FileLock`；创建新 Session 使用单独的 creation lock，避免 CLI 与 Gateway 同时分配相同标识。

#### 9.2.2 运行时隔离

一次 turn 的隔离由多层共同完成：

```text
session_id
   ├─ SessionStore.get/save                  对话与 metadata
   ├─ Gateway _active_turns[session_id]      活动 turn 与取消事件
   ├─ WorkspaceManager binding[session_id]   文件系统根
   ├─ AUTO / UNLIMITED maps                  模式
   ├─ RuntimeAgentClient backend metadata    原生/Pi
   ├─ attachment directory                  附件
   ├─ Cron payload.session_key              定时任务回到原 Session
   └─ Rollback binding/checkpoints           工作区与对话回退
```

Gateway 对同一 `session_id` 的第二个并发请求返回冲突，避免两条 Agent Loop 交错追加消息；不同 Session 可以并行运行。工作线程启动时还会设置 thread-local 当前 Session，Tool 层则通过 `ContextVar` 绑定 RequestContext、WorkspaceScope 和文件读写状态，所以共享的 Registry 不会把 A 会话的工具上下文泄漏给 B。

需要特别注意两种“比 Session 更宽”的锁：

- `CompactionWorker` 当前一次只执行一个后台压缩任务，属于吞吐控制，不改变摘要最终仍按 Session 写回；
- 两个 Session 若绑定同一个物理 workspace，Rollback 会按规范化 workspace 根串行化 turn，防止各自的文件快照互相覆盖。

#### 9.2.3 压缩、分叉与恢复下的隔离

Compaction 不删除 `messages`，只移动 `last_consolidated` 并更新 `summary`；因此 Session 的原始审计历史仍在同一文件中。`get_history()` 只选择当前需要发送的窗口，并清理孤立 tool result、过大的工具输出和多模态残留，不会改写别的 Session。

`fork_session_before_user_index()` 会复制目标用户消息之前的历史，并重新计算压缩边界。运行时崩溃恢复则依靠 `pending_user_turn` 和 `runtime_checkpoint`：已完成工具结果可以恢复，尚未完成的工具调用会被补成明确失败结果，避免恢复后留下不合法的 tool-call 序列。

### 9.3 Agent Loop

#### 9.3.1 外层事务与后端路由

Agent Loop 有两层职责：

```text
run_agent_turn
   ├─ WorkspaceRollbackManager.turn_guard
   │    └─ 创建“用户消息前”检查点并持有隔离锁
   └─ _run_agent_turn_unlocked
        ├─ RuntimeAgentClient.run_agent_turn 可接管完整 turn
        └─ 普通 LLMClient 进入原生 Think-Act-Observe 循环
```

Gateway 和 CLI 注入的是 `RuntimeAgentClient`。首次进入时，Loop 发现客户端具有 `run_agent_turn()`，把完整 turn 交给它分流：

- 后端为 `pi`：调用 `PiAgentClient.run_agent_turn()`；
- 后端为 `sjtuclaw`：`RuntimeAgentClient` 用普通 `LLMClient` 再进入原生循环。普通客户端没有完整-turn 方法，因此不会再次分流，也不会递归死循环。

Rollback 检查点创建在分流之前，所以无论最终走原生还是 Pi，用户看到的“这一轮”都具有相同的回退边界。

#### 9.3.2 原生 Think-Act-Observe 状态机

原生循环的核心状态转移如下：

```text
[追加 user 消息并 fsync]
          |
          v
   [Build Context] <------------------------------------+
          |                                             |
          v                                             |
   [LLM + tool definitions]                             |
       /             \                                  |
  final reply       tool_calls                           |
      |               |                                 |
      |        [写 assistant tool_calls]                 |
      |               |                                 |
      |        [审批 / Skill 选择 / 执行工具]             |
      |               |                                 |
      |        [写 matching tool results] ---------------+
      v
[写 assistant final]
      |
      +--> FinalEvent --> CompactionWorker.submit_if_needed()
```

每轮迭代都会重新调用 `ContextBuilder`，因此工具结果、Skill 注入和最新摘要会进入下一次模型判断。模型返回 tool call 时，系统先持久化带原生 `tool_calls` 的 assistant 消息，再为每个 call 写入相同 ID 的 tool 消息，保证 OpenAI 兼容协议中的配对关系。

Loop 还维护四类保护：

- 迭代次数上限与工具调用总数上限；
- 重复调用/无进展检测；
- 审批连续拒绝计数；
- cooperative cancellation：在迭代边界、模型返回后、工具执行前后检查取消事件。

同步 LLM 请求本身不能被强制打断，但 `/stop` 设置取消事件后，迟到的模型结果不会继续触发工具。所有异常、未知工具、参数校验失败和审批拒绝都会转换为明确的观察结果或最终错误，而不是让消息链停在半个 tool call。

#### 9.3.3 Context 装配关系

原生主循环每次调用 `ContextBuilder.build_messages()`，其稳定顺序是：

```text
identity/system
→ soul
→ tool contract/bootstrap/AGENTS.md
→ Memory 轻量索引
→ Skill 索引与 always Skill 全文
→ Session summary
→ 未压缩对话窗口
→ 仅附着在最新用户消息上的 runtime context/media
```

稳定块会按 Memory/Skill version 和 workspace 缓存；易变的时间、当前 Session、workspace 等 runtime context 只放到最新用户消息附近，避免破坏提供商的 prompt cache。发送前 `ContextGovernor` 可以在副本上补齐缺失 tool result、裁剪超大历史或触发紧急压缩，但不修改持久化原文。

### 9.4 Compaction 系统

#### 9.4.1 压缩的真实含义

SJTUClaw 的压缩不是删除消息，而是建立一个逻辑读取边界：

```text
messages: [旧消息 0 ... N-1][近期消息 N ... end]
                    ^
             last_consolidated = N

发送上下文 = summary(旧消息) + messages[N:]
审计/回退   = messages[0:] 仍完整保留
```

`compact_session()` 只计算 `CompactionResult`；`apply_compaction_result()` 才更新 `summary`、推进 `last_consolidated` 并增加 `revision`。这种“计算与提交分离”使后台线程可以在不长时间占有 Session 锁的情况下调用 LLM。

#### 9.4.2 触发与安全切分

自动压缩只在完整 turn 结束后提交，并要求未压缩消息超过 token 阈值。当前没有“会话空闲后自动截断”路径。手动 `/compact` 使用 `force=True` 绕过常规预算检查，但仍必须存在可安全归档的旧前缀。

切分点不能落在任意消息之间。`has_compactable_prefix()` 会：

1. 保留一个近期 token/消息窗口；
2. 向前对齐到完整的 user turn；
3. 避免把 assistant tool call 与其 tool result 拆开；
4. 在消息数量过少时拒绝压缩。

送给摘要 LLM 前，旧工具输出会被裁剪，已有 summary 会一起进入提示词，以形成增量摘要。LLM 返回空摘要或报错时抛出 `CompactionError`，原 Session 不发生改变，并进入失败冷却，避免每个 turn 都重复轰炸摘要接口。

#### 9.4.3 后台提交与 ABA 防护

`CompactionWorker` 的关键流程是：

```text
Session 锁内:
  snapshot(messages, summary, revision)
       |
       v
锁外调用摘要 LLM
       |
       v
Session 锁内:
  当前 revision == snapshot revision ?
       ├─ 否：丢弃过期结果
       └─ 是：apply + save
```

revision 检查不仅防止“压缩时又来了新消息”，也防止 ABA：例如用户回退到旧状态后又写入看似相同数量的消息，revision 仍会不同，旧摘要无法覆盖新时间线。Gateway 给 worker 的过滤器还会跳过 Pi Session，因为 Pi 拥有自己的会话压缩机制。

### 9.5 Memory 系统

#### 9.5.1 为什么 Memory 跨 Session

Memory 的目标是保存用户偏好、项目事实和决策，而不是复制聊天记录，因此它是 Runtime 级全局知识库。文件保存在：

```text
data/memory/
├─ user_preference/
├─ project/
├─ fact/
├─ decision/
└─ general/
```

每条记忆是带 YAML frontmatter 的 Markdown，包含类别、标签、重要度、创建/更新时间、召回统计和 `source_session_id`。来源字段用于追踪“这条事实来自哪里”，但不会限制它只能被原 Session 使用。

#### 9.5.2 写入、索引与召回

`MemoryStore` 启动时扫描文件建立内存索引，写入采用临时文件 + 原子替换；add/update/delete 会增加 `version`，使 `ContextBuilder` 的 Memory 索引缓存失效。

模型不会在每个 turn 获得所有 Memory 正文。ContextBuilder 只注入统计、使用说明和少量近期条目，模型需要时调用 `recall`。召回排序综合：

- 标签精确/部分匹配；
- 查询词在标题、正文中的完整或分词命中；
- 中文字符重叠；
- 用户偏好类别加权；
- importance、创建时间、历史召回次数和最近召回时间。

这种“轻索引 → 主动 recall → 返回少量正文”的渐进检索，既控制 token，也降低无关旧知识干扰当前 Session。

#### 9.5.3 Memory 与其他模块

- `remember` 是 write 安全级别，经过 Agent Loop 审批；`recall` 是只读工具；
- Tool 的 RequestContext 提供当前 `session_id`，用于写入来源；
- ReflectionManager 扫描有变化的 Session 摘要和近期消息，通过辅助 LLM 提取可复用事实，再写入全局 Memory；
- ContextBuilder 只读取 Memory 索引，不直接修改 Store；
- Rollback 回退 Session 和 workspace，不回退全局 Memory。用户若已批准写入一条长期记忆，该记忆是独立事实，需要用 Memory 管理命令单独删除。

### 9.6 Tool 系统

#### 9.6.1 定义、注册、展示、执行

每个 `Tool` 包含名称、描述、JSON Schema、handler、`safety_level`、并发标志和结果长度上限。生命周期为：

```text
register_all_tools()
   → ToolRegistry.register() 校验名称/schema/重复项
   → list_definitions() 转成 provider tool schema
   → LLM 返回 name + arguments
   → Agent Loop 决定是否审批
   → execute_by_name()
        1. 复制参数
        2. prepare_call 前置检查（若配置）
        3. 轻量 JSON Schema 校验
        4. handler 执行
        5. ToolResult 不变量校验
        6. 截断过长 content/error
   → tool result 写回 Session
```

`ToolResult` 强制“成功只有 content、失败只有 error”，让 Agent Loop 不必猜测半成功状态。未知工具、错误参数、handler 异常都会规范化为失败结果。

#### 9.6.2 安全级别不是唯一防线

安全控制分布在三层：

1. `safety_level` 告诉 Agent Loop 是否需要审批；
2. Agent Loop 根据 AUTO、UNLIMITED 和工具类型执行 fail-closed 策略；
3. handler/WorkspaceManager 在真正接触文件系统时再次校验路径。

因此，AUTO 只会跳过结构化 write 的“人工确认”，不会跳过 workspace 边界；Shell 即使在 AUTO 下也必须显式审批。`approval_handler` 缺失时 write/shell 默认拒绝。

#### 9.6.3 共享 Registry 如何保持 turn 隔离

Registry 是全局共享对象，但 turn 特有状态不保存在 Tool 实例的普通字段中。`claw/tools/base.py` 使用 `ContextVar` 传递：

- `RequestContext`：session、channel、chat 等来源；
- `FileStates`：read-before-write 等文件状态；
- `WorkspaceScope`：当前工作区范围。

CronTool 也用独立 ContextVar 保存 `session_key`、来源渠道和“当前是否由 Cron 触发”。Gateway 工作线程在执行前绑定这些上下文，结束后 reset，避免线程池复用造成状态串线。Pi 宿主工具桥接最终仍调用同一 `ToolRegistry`，因此参数校验、结果规范化和 workspace handler 不会因换后端而消失。

### 9.7 Workspace 系统

#### 9.7.1 绑定与并发持久化

`WorkspaceManager` 维护 `session_id -> absolute Path`，持久化在 `data/workspace/bindings.json`。更新绑定时不是把进程内旧快照直接覆盖到磁盘，而是在进程锁和文件锁内重新读取最新文件、合并当前 Session 的变化，再原子替换，从而避免 CLI 和 Gateway 同时运行时互相抹掉绑定。

绑定路径必须是存在的目录。目录后来被移走时，绑定信息仍保留，但使用时会返回明确的“workspace 不存在”错误，等待用户修复或重新绑定。

#### 9.7.2 路径解析是强制边界

沙箱模式下的解析规则为：

```text
输入相对路径
   → workspace / input
   → resolve() 消解 .. 和符号链接
   → relative_to(workspace.resolve())
       ├─ 成功：允许
       └─ 失败：拒绝越界
```

绝对路径默认被拒绝；`../`、符号链接逃逸和指向 workspace 外部的解析结果也会失败。文件读写、下载、附件落盘和 Shell 的路径预检都依赖这条边界，但各 handler 仍负责自己的类型、大小、命令和网络规则。

#### 9.7.3 Workspace 与上下文、回退、Pi

- ContextBuilder 把当前 workspace 和 `AGENTS.md`/bootstrap 信息加入系统上下文；
- Rollback 以 workspace 为快照范围，两个 Session 共享同一目录时按目录根加锁；
- Pi Session 若绑定 workspace，会把它作为 Pi 子进程 `cwd`；
- UNLIMITED 使 `resolve()` 绕过 workspace 边界，但不会自动关闭审批，也不会扩大 Rollback 的快照范围。

### 9.8 Skill 系统

#### 9.8.1 Registry 与可用性

`SkillRegistry` 扫描 `skills/` 及一层分类目录中的 `SKILL.md`，解析 frontmatter、正文、依赖的二进制和环境变量。Skill 可以存在但不可用：例如缺少必需命令或配置时，Registry 会保留其元数据并标记原因，便于 UI 展示，而不是在执行到一半才失败。

Registry 用目录 mtime/version 检测变化并热重载。使用次数、最近使用时间等 telemetry 放在 sidecar 状态中，不回写用户维护的 `SKILL.md`。

#### 9.8.2 三阶段渐进披露

Skill 内容按需进入上下文：

```text
阶段 1：ContextBuilder 注入 Skill 名称、描述、可用性索引
阶段 2：模型调用 skills_list / skill_view 查看目标 Skill 或引用文件
阶段 3：显式 /skill <name> <task> 将完整 Skill 转成注入用户消息
```

标记为 always 的 Skill 会在阶段 1 直接注入全文。普通 Skill 不会全部展开，避免大量说明长期占用上下文。`skill_view` 只允许读取目标 Skill 目录内的文件，并限制路径、类型和大小。

代码还保留 `safety_level="skill_select"` 的 `use_skill` 特殊分支，供注册该工具的扩展运行时做“模型选择 Skill → 用户确认 → 注入”；默认注册链主要使用 `skills_list`、`skill_view`、`skill_manage` 和显式 `/skill`。

#### 9.8.3 管理与调用关系

`skill_manage` 属于 write 工具，创建、修改、删除或安装包都要经过审批。安装 ZIP/TAR 时会验证归档路径、文件数量、扩展名和大小，并通过 staging + swap 发布，避免半安装状态。

显式 `/skill` 的调用链是：

```text
CLI/Gateway 解析命令
   → 校验 Skill 可用
   → run_agent_turn(skill_source="explicit", skill_name=...)
   → ContextBuilder.build_skill_injection_message()
   → Session 记录 skill_usage
   → SkillRegistry.record_use()
   → 后端执行用户任务
```

Pi 后端接到显式 Skill 时会把请求转换为 Pi 可识别的 Skill prompt，同时运行时生成的 Skill/Tool 清单仍用于桥接宿主能力。

### 9.9 Cron 系统

#### 9.9.1 数据模型与调度语义

`CronSchedule` 支持 `at`、`every`、`cron` 三种且必须恰好选择一种。`CronPayload` 保存：

- `agent_turn` 或 `system_event`；
- `session_key`；
- 原始 channel、chat id 和 metadata；
- `depends_on` 依赖任务。

`CronService` 加载持久化任务后计算最近一次 `next_run_at`，定时器只等待最近任务并定期 heartbeat。损坏的 jobs 文件会被隔离，服务不会用空列表直接覆盖可能仍可恢复的数据。

#### 9.9.2 claim、执行与崩溃恢复

任务到期时，Service 先把下一次时间/运行 claim 持久化，再调用回调：

```text
due job
  → 写 run_claim 与 next_run_at
  → 原子保存 jobs
  → 深拷贝任务
  → 注入依赖任务最新输出
  → 调用 dispatcher
  → 保存结果、历史与 Markdown 输出
```

先 claim 后执行可防止服务重启后重复抢占同一到期任务。一次性任务使用带 TTL 的 at-most-once 语义；周期任务在执行前推进下一次时间。运行历史和输出有保留上限，失败次数过多或一次性任务完成后会按策略禁用/删除。

#### 9.9.3 回到同一 Agent Runtime

`create_cron_dispatcher()` 不实现第二套 Agent。它恢复 payload 的 Session/渠道上下文，绑定 thread-local 与 Cron ContextVar，然后调用：

```text
run_agent_turn(
    session_key,
    scheduled_prompt,
    input_event="cron_trigger",
    ...
)
```

因此 Cron turn 与人工消息共享 Session、Workspace、后端、Tool、Approval、Rollback 和 Compaction。`input_event` 让 UI 可以把调度提示标记为系统来源，但该消息仍存在于模型上下文和审计历史中。

CronTool 在普通 turn 中允许 add/list/remove；创建任务时必须有当前 Session 绑定，并拒绝在 Cron 触发的 turn 内再创建 Cron，避免递归任务指数扩散。Heartbeat 是一个特殊系统任务，也通过 dispatcher 进入同一主链。

### 9.10 Gateway

#### 9.10.1 组合根

`claw/gateway/server.py` 是 HTTP/桌面运行方式的 composition root。模块初始化阶段创建一套共享组件：

```text
SessionStore ─┐
MemoryStore  ─┤
SkillRegistry ├─> ContextBuilder ─┐
WorkspaceMgr ─┘                   │
ToolRegistry + ApprovalManager ───┼─> run_agent_turn
RollbackManager ──────────────────┤
RuntimeAgentClient ────────────────┘
CronService / Reflection / CompactionWorker / Channels
```

应用 lifespan 启动 Cron、Reflection、QQ、Pet 等后台服务，关闭时先停止接收新工作，再等待/停止后台组件，避免进程退出时留下半写入状态。

#### 9.10.2 请求与 SSE

普通 `/chat` 在线程池工作线程中运行完整 turn，等待最终文本后返回 JSON。`/chat/stream` 则建立线程安全队列：

```text
Agent worker --TurnEvent--> queue --SSE encoder--> browser
```

Thinking、ToolCallStart、ToolCallEnd、Approval 和 FinalEvent 可以逐步到达前端；keepalive 防止代理误判连接空闲。Agent 不占用 FastAPI 事件循环，所以模型运行期间 `/stop` 和审批决定接口仍可响应。

`_active_turns[session_id]` 保存取消事件和任务状态。同一 Session 再次发送消息会冲突，不同 Session 允许并行。斜杠命令先经过白名单解析：已识别命令在本地执行，未知或伪造的控制命令不会混入 LLM 主对话链。

#### 9.10.3 边界防护

Gateway 中间件依次承担来源/API token 检查、滑动窗口限流、按实际流式读取字节数限制请求大小、请求 ID 与日志。上传使用分块写入和超限回滚，附件目录按 Session 分开，读取时再次校验所属关系。静态 WebUI 最后挂载，避免吞掉 API 路由。

审批管理器可以在 Agent 工作线程中等待用户决定，而 HTTP 主循环继续服务。若审批回调异常或不存在，Agent Loop 按 fail-closed 记录拒绝结果。

### 9.11 Rollback

#### 9.11.1 快照内容与存储

`WorkspaceRollbackManager` 使用两类存储：

- SHA-256 内容寻址对象库保存文件内容，相同内容只存一次；
- SQLite（WAL）保存 binding generation、checkpoint、文件 manifest 和 operation 状态。

启用回退时会创建新的 binding generation 和 baseline。每次用户 turn 开始前，`turn_guard` 生成检查点，内容包括 workspace manifest、压缩的 Session 快照、用户消息 ID 关联以及是否为 partial。

`.git`、`node_modules`、虚拟环境、缓存等目录默认排除，避免快照巨大且难以稳定恢复。UNLIMITED turn 被标记为 partial，因为 workspace 之外的修改无法被当前快照捕获。

#### 9.11.2 锁顺序与共享目录

Rollback 先持有 Session 锁，再持有规范化 workspace 根锁。这个顺序保证：

- 同一 Session 不能同时创建/应用两个检查点；
- 不同 Session 共用一个目录时，文件修改仍然串行；
- Session 的对话快照与对应 workspace 文件状态处于同一 turn 边界。

#### 9.11.3 两阶段回退与补偿

预览只比较当前 manifest 与目标 checkpoint，报告将恢复、删除的文件和消息数，不写对象。真正回退采用可恢复状态机：

```text
创建 safety checkpoint
   → operation = PREPARED
   → 应用目标文件 manifest
   → operation = FILES_APPLIED
   → 恢复 Session 快照并 fsync
   → operation = COMMITTED
```

任一步失败会进入 `COMPENSATING`，用 safety checkpoint 恢复操作前状态。进程重启时会扫描未完成 operation 并幂等恢复。对象垃圾回收采用 mark-and-sweep，只删除没有 checkpoint 引用的对象。

恢复 Session 时会清理 pending/runtime checkpoint、增加 revision，并旋转 `pi_session_generation`。增加 revision 让正在运行的 CompactionWorker 结果失效；旋转 Pi generation 则保证下次进入 Pi 时不会继续使用包含“已被回退对话”的旧 Pi 会话。

### 9.12 AUTO 与 UNLIMITED 模式

两种模式都按 Session 生效，但解决的是完全不同的问题：

| 模式 | 改变什么 | 不改变什么 | 持久化 |
|------|----------|------------|--------|
| AUTO | workspace 内结构化 write 可跳过人工审批 | Shell 仍审批；workspace 边界仍有效 | 仅内存 |
| UNLIMITED | 路径解析可访问 workspace 外 | write/shell 仍强制审批；回退范围仍是绑定 workspace | 仅内存 |

原生 Agent Loop 的决策可以概括为：

```text
read/network/download
    → 按工具自身边界执行

write/shell
    → approval_handler 缺失：拒绝
    → shell：始终显式审批
    → unlimited：始终显式审批
    → auto 且为结构化 write：跳过人工审批
    → 其他：显式审批
```

当 AUTO 与 UNLIMITED 同时开启时，UNLIMITED 的强制审批优先，AUTO 不能自动批准越界写入。即使结构化 write 被 AUTO 放行，handler 仍调用 WorkspaceManager 做最终路径检查。

Gateway 用 `_auto_mode[session_id]` 保存 AUTO，WorkspaceManager 用 `_unlimited_sessions` 保存 UNLIMITED；CLI 的 `RuntimeState` 也按 Session 保存 AUTO。两者均不写入 Session metadata，重启后恢复为安全默认值。删除/切换相关 Session 时也会清理内存模式，避免状态错误继承。

### 9.13 Pi 后端切换

#### 9.13.1 选择状态与迁移

后端选择保存在 `session.metadata["agent_backend"]`，合法值为 `sjtuclaw` 或 `pi`。`AGENT_BACKEND` 只决定尚未初始化 Session 的默认值；`initialize_session_backends()` 会把默认值冻结到旧 Session，之后修改环境变量不会把所有现有会话一起切换。

`/pi on` 的顺序是先验证 Pi 配置/命令可用，再调用 `set_session_backend()`；失败时不修改 Session。切换回原生后端前也会确认原生 LLM 配置完整。

#### 9.13.2 generation 与 handoff

Pi 自己维护追加式持久会话。若从原生切回一个旧 Pi 进程，旧进程并不知道中间由原生后端完成的对话。为避免恢复过期分支，切换到 Pi 时会：

1. 生成新的 `pi_session_generation`；
2. 清除 owner/initialized generation；
3. 首个 Pi turn 读取 SJTUClaw 的已有 messages 和 summary；
4. 生成 authoritative handoff prompt；
5. 标记当前 generation 已初始化。

Pi session token 由 SJTUClaw `session_id + generation` 派生，因此同一 Session 的旧 Pi 文件不会被误续接。Rollback 同样旋转 generation，形成一条新的 Pi 时间线。

#### 9.13.3 完整 turn、工具与事件桥接

Pi 路径不是“用 Pi 只生成一段文本”，而是由 Pi 完成自己的模型、工具循环、重试、上下文压缩和持久化。Python 侧负责：

- 选择绑定 workspace 作为 `cwd`；
- 生成本轮 prompt、工具清单和桥接 token；
- 启动 Pi JSONL RPC 子进程；
- 通过 TypeScript extensions 提供 permission gate、provider 和 host tools；
- 把 Pi 的 user/tool/assistant 事件记录回 SJTUClaw Session；
- 把事件转换为与原生 Loop 相同的 TurnEvent，供 SSE/CLI/QQ 消费。

宿主工具桥接最终执行共享 `ToolRegistry`，并把需要变更状态的操作交给审批链。这样换后端只替换“谁拥有 Agent Loop”，不会替换 Session、Workspace、Rollback、Gateway 和渠道层。

#### 9.13.4 Compaction 的分叉

原生 Session 的自动压缩由 `CompactionWorker` 完成；Pi Session 被该 worker 过滤。对 Pi Session 执行 `/compact` 时，`RuntimeAgentClient.compact_session()` 向对应 Pi RPC 发送 `compact` 请求，使用 Pi 的原生摘要和会话文件。

辅助 LLM 仍可能存在：`RuntimeAgentClient.chat()` / `chat_with_tools()` 委托给原生兼容客户端，供 Reflection、标题等非 Pi 主 turn 功能使用。如果只配置 Pi 而未配置辅助 LLM，这些辅助功能会明确报“辅助 LLM 未配置”，不会偷偷改用主 turn。

### 9.14 端到端流程与模块协作

#### 9.14.1 人工消息

```text
WebUI/CLI/QQ
  → Gateway/REPL 解析命令与 session_id
  → 活动 turn 检查
  → 绑定 RequestContext/CronContext/workspace
  → Rollback.turn_guard 创建检查点
  → RuntimeAgentClient 读取 Session backend
      ├─ sjtuclaw
      │   → ContextBuilder
      │   → LLM
      │   → ToolRegistry ↔ ApprovalManager ↔ WorkspaceManager
      │   → SessionStore
      │   → CompactionWorker
      └─ pi
          → handoff/runtime files/JSONL RPC
          → Pi tool loop ↔ host ToolRegistry/Approval
          → SessionStore
          → Pi native compaction
  → TurnEvent
  → JSON/SSE/CLI/QQ 输出
```

#### 9.14.2 定时消息

```text
CronService 到期
  → claim 先持久化
  → dispatcher 恢复 session/channel
  → run_agent_turn(input_event="cron_trigger")
  → 与人工消息相同的后端、工具、workspace、回退和压缩链
  → 结果返回来源渠道
  → CronService 保存输出与历史
```

#### 9.14.3 写文件并回退

```text
turn 开始
  → Rollback checkpoint(旧文件 + 旧 Session)
  → Tool write 请求
  → AUTO/UNLIMITED/类型共同决定审批
  → WorkspaceManager 校验最终路径
  → handler 写文件
  → tool result + final reply 写入 Session

/rollback
  → preview 差异
  → safety checkpoint
  → 恢复文件 manifest
  → 恢复 Session
  → revision 变化使旧 compaction 失效
  → Pi generation 变化使旧 Pi 分支失效
```

#### 9.14.4 最重要的模块边界

维护代码时应保持以下关系：

1. 入口层可以解析命令和转换事件，但完整对话必须进入 `run_agent_turn()`。
2. SessionStore 是对话真相源；ContextBuilder 只构建发送视图，ContextGovernor 只修改临时副本。
3. Compaction 改变摘要读取边界，不删除原始历史。
4. Memory 是全局知识层，Session 是隔离对话层；二者不能互相代替。
5. ToolRegistry 负责能力协议，Agent Loop 负责审批编排，WorkspaceManager/handler 负责最终资源边界。
6. Cron 是入口和调度器，不是另一套 Agent Runtime。
7. Gateway 是组合根和传输层，不应复制业务状态机。
8. Rollback 同时恢复 workspace 与 Session；revision 和 Pi generation 把恢复动作传播给异步压缩与外部后端。
9. AUTO 只缩短审批流程，UNLIMITED 只放宽路径范围；两者都不能绕开其余安全层。
10. Pi 替换完整 Agent 后端，但复用 Session、Gateway、Workspace、Rollback、渠道和宿主工具边界。

---

## 十、测试体系

### 10.1 后端测试

测试文件位于 [tests/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/)，使用 pytest：

```bash
python -m pytest tests/ -v
```

主要测试覆盖：

| 测试文件 | 覆盖范围 |
|----------|----------|
| `test_core.py` | 核心功能 |
| `test_compaction.py` | 上下文压缩 |
| `test_reflection.py` | 每日反思 |
| `test_workspace_rollback.py` | 工作区回退 |
| `test_cron_integration.py` | Cron 集成 |
| `test_skill_management.py` | Skill 管理 |
| `test_pet.py` / `test_pet_replies.py` / `test_pet_command.py` | 桌宠 |
| `test_pi_integration.py` / `test_pi_real_prompt.py` | Pi 集成 |
| `test_unlimited_approval.py` | Unlimited 模式审批 |
| `test_security_hardening.py` | 安全加固 |
| `test_session_store_concurrency.py` | Session 存储并发 |
| `test_gateway_fixes.py` / `test_gateway_rollback.py` | Gateway |
| `test_cli_rollback_integration.py` | CLI 回退集成 |
| `test_cli_setup.py` / `test_cli_repl_cleanup.py` | CLI 配置向导与退出清理 |
| `test_commands_hardening.py` / `test_skill_cron_hardening.py` | 命令、Skill 与 Cron 安全回归 |
| `test_explicit_skill_entrypoints.py` | Skill 显式调用 |
| `test_encoding.py` | 编码处理 |

### 10.2 前端测试

```bash
cd webui
npx vitest run
```

测试文件位于 [webui/src/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/)，与组件同目录，包括 `Sidebar.test.tsx`、`ThreadComposer.test.tsx`、`ThreadShell.test.tsx`、`ThreadViewport.test.tsx`、`api.test.ts`、`commandState.test.ts`、`commands.test.ts`、`utils.test.ts`、`useDragScroll.test.tsx`、`PetSelectionIntegration.test.tsx`。

---

## 附录：关键不变量约束

整个 SJTUClaw 代码库遵循以下不变量：

1. **`run_agent_turn` 是主对话 turn 的统一入口**——CLI/Gateway/Scheduler/QQ/Heartbeat 都必须路由到这里；辅助 LLM 任务不冒充对话 turn。
2. **`ContextBuilder` 统一装配原生主循环 messages**——入口层不自行拼主对话上下文；Pi 与辅助任务使用各自明确的 prompt 路径。
3. **`token_counter` 是唯一 token 估算源**——所有模块都走这里，避免散落的字符数估算。
4. **compaction 只处理 `session.summary` 和 `session.messages` 边界**——绝不触碰 system prompt/soul/memory store。
5. **`ContextGovernor` 永不修改持久化历史**——只准备一份副本用于发送。
6. **compaction 失败时不能删除旧 messages**——`CompactionError` 保证 session 完全未变。
7. **`CompactionWorker` 用 revision 守卫防止 ABA 问题**——过期结果直接丢弃。
8. **`approval_handler is None` 时 write/shell fail-closed**——永远拒绝，无法通过省略回调绕过。
9. **workspace 边界强制**——写入/shell/下载/附件工具全部通过 `workspace_manager.resolve` 或路径预扫描防止越界。
10. **原子写入**——SessionStore、MemoryStore、SkillManager、CronService 等均采用 tmp + replace 原子写入策略。
11. **JSONL 独立持久化**——每个 Session 采用一行 metadata + 每行一消息的 JSONL，并通过整文件原子替换保存；单行损坏只跳过一条消息。
12. **ContextVar per-turn 绑定**——RequestContext、FileStates、WorkspaceScope 通过 contextvars 实现异步安全的 per-turn 上下文传递。

---

> 本文档基于 SJTUClaw 项目源码生成，覆盖 `SJTUClaw.md` 中 Step 0–Step 9 的所有功能要求以及开发者新增的 Pet、Rollback、Reflect、Auto/Unlimited 模式、Pi Agent 接入等功能。所有代码引用均提供可点击的文件链接，便于跳转到源码位置。
