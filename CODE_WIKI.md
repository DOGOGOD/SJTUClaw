# SJTUClaw Code Wiki

> 面向个人与教学场景的本地 AI Agent Runtime
>
> 本文档是 SJTUClaw 项目仓库的结构化代码百科，覆盖项目整体架构、模块职责、核心功能实现方式、关键类与函数说明、依赖关系以及运行方式。

---

## 目录

1. [项目概览](#1-项目概览)
2. [整体架构](#2-整体架构)
3. [目录结构与模块职责](#3-目录结构与模块职责)
4. [核心功能实现详解](#4-核心功能实现详解)
   - 4.1 [统一 Agent Loop](#41-统一-agent-loop)
   - 4.2 [上下文构建与压缩](#42-上下文构建与压缩)
   - 4.3 [工具系统与安全审批](#43-工具系统与安全审批)
   - 4.4 [Workspace 沙箱与回退系统](#44-workspace-沙箱与回退系统)
   - 4.5 [会话持久化与崩溃恢复](#45-会话持久化与崩溃恢复)
   - 4.6 [长期记忆与每日 Reflection](#46-长期记忆与每日-reflection)
   - 4.7 [Skill 系统](#47-skill-系统)
   - 4.8 [定时任务与 Heartbeat](#48-定时任务与-heartbeat)
   - 4.9 [Gateway REST API 与 SSE](#49-gateway-rest-api-与-sse)
   - 4.10 [QQ Bot 渠道](#410-qq-bot-渠道)
   - 4.11 [桌面宠物](#411-桌面宠物)
   - 4.12 [Web UI 前端](#412-web-ui-前端)
   - 4.13 [Windows 桌面打包](#413-windows-桌面打包)
5. [关键类与函数参考](#5-关键类与函数参考)
6. [依赖关系](#6-依赖关系)
7. [项目运行方式](#7-项目运行方式)
8. [测试与构建](#8-测试与构建)

---

## 1. 项目概览

**SJTUClaw** 是一个把多轮对话、工具调用、长期记忆、Skill、定时任务和桌面宠物整合为一体的本地 AI Agent 工作台。它提供多种入口（Windows 桌面应用、CLI、Web UI、REST API、QQ Bot），适合学习 Agent Runtime，也可用于搭建个人自动化助手。

### 核心能力

- **统一 Agent Loop**：所有入口共享 [run_agent_turn()](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)，保证一致的行为。
- **工具调用与安全审批**：文件读写、Shell、联网、下载、记忆、Skill、Cron 工具，按安全级别控制执行。
- **可控执行模式**：按 Session 隔离的 AUTO 与 UNLIMITED 模式。
- **上下文与长期记忆**：Session 持久化、上下文压缩、Markdown 记忆与每日 Reflection。
- **Workspace 回退**：逐回合检查点 + SQLite + SHA-256 对象库。
- **Skill 系统**：通过 `SKILL.md` 组织可复用工作流。
- **多入口与实时反馈**：Web UI 通过 SSE 展示事件，QQ Bot 支持内联审批。
- **本地化时间与定时任务**：自动识别系统时区，支持 `CLAW_TIMEZONE` 覆盖。
- **Windows 桌面应用**：pywebview 承载 Web UI，PyInstaller + Inno Setup 打包。
- **桌面宠物**：独立窗口、状态展示、随 Gateway 启动。

### 技术栈速览

| 层次 | 技术 |
|------|------|
| 后端 | Python 3.11、FastAPI、Uvicorn |
| LLM | OpenAI 兼容 API、httpx、aiohttp |
| Agent | 自研 Agent Loop、ToolRegistry、上下文压缩、审批管理 |
| 存储 | JSONL Session、SQLite 回退元数据、SHA-256 对象库、Markdown + YAML 记忆 |
| 调度 | croniter、Heartbeat |
| 前端 | React 18、TypeScript、Vite、Tailwind CSS |
| 渲染 | react-markdown、KaTeX、react-syntax-highlighter |
| 桌面 | pywebview、PyInstaller、Inno Setup 7、tkinter、Pillow |
| 测试 | pytest、Vitest |

---

## 2. 整体架构

SJTUClaw 采用**分层架构**，所有用户入口最终汇入同一套 Agent Runtime。

```
┌──────────────────────────────────────────────────────────────────┐
│                        用户入口层 (Entry Points)                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │ Windows  │ │   CLI    │ │  Web UI  │ │ REST API │ │ QQ Bot │ │
│  │ Desktop  │ │ sjtuclaw │ │  (Vite)  │ │ FastAPI  │ │ WS API │ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └───┬────┘ │
└───────┼────────────┼────────────┼────────────┼───────────┼──────┘
        │            │            │            │           │
        ▼            ▼            ▼            ▼           ▼
┌──────────────────────────────────────────────────────────────────┐
│                        Gateway 服务层                            │
│  claw/gateway/server.py  (FastAPI + 中间件 + SSE + 静态资源)     │
│  ┌────────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐ │
│  │ RateLimit  │ │ ReqSize  │ │ Security │ │ Logging │ │  CORS  │ │
│  └────────────┘ └──────────┘ └──────────┘ └─────────┘ └────────┘ │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                       Agent Runtime 核心                         │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  claw/agent/loop.py  →  run_agent_turn()                    │ │
│  │  Think-Act-Observe 循环 + 工具调用 + 审批 + 事件流           │ │
│  └────┬───────────┬─────────────┬─────────────┬────────────────┘ │
│       │           │             │             │                  │
│  ┌────▼────┐ ┌────▼────┐ ┌──────▼─────┐ ┌─────▼──────┐           │
│  │ Context │ │  Tools  │ │ Approval   │ │  Health    │           │
│  │ Builder │ │ Registry│ │ Manager   │ │  Monitor   │           │
│  └────┬────┘ └────┬────┘ └───────────┘ └────────────┘           │
│       │           │                                                │
│  ┌────▼────┐ ┌────▼────────────────────────────────────┐         │
│  │ LLM     │ │ 工具：read/write/shell/web/memory/skill  │         │
│  │ Client  │ │       /cron/download/attachment          │         │
│  └─────────┘ └──────────────────────────────────────────┘         │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                        持久化与调度层                            │
│  ┌────────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌─────────┐ │
│  │  Session   │ │ Memory   │ │Workspace │ │ Cron    │ │Reflection│ │
│  │  JSONL     │ │ Markdown │ │ Rollback │ │ Service │ │ Manager │ │
│  │            │ │ + YAML   │ │ SQLite   │ │         │ │         │ │
│  └────────────┘ └──────────┘ └──────────┘ └─────────┘ └─────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### 关键设计原则

1. **单一 Agent Loop 入口**：所有渠道（CLI/Gateway/QQ/Cron/Heartbeat）必须通过 [run_agent_turn()](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)，绝不直接调用 `LLMClient`。
2. **不可变持久化历史**：上下文压缩只推进 `last_consolidated` 投影边界，绝不删除原始消息，支持回退和审计。
3. **Defense in Depth 安全边界**：Workspace 沙箱由多层独立检查共同保证（WorkspaceManager、工具处理器、Shell 预扫描）。
4. **快照式并发**：CompactionWorker 在短暂加锁后立即释放，慢速 LLM 调用在锁外执行，永不丢失新消息。
5. **CJK 友好的 Token 估计**：tiktoken `o200k_base`，缺失时使用 CJK 字符 ×2 + 其他字符 ×4 的保守启发式。
6. **Prompt-cache 友好**：稳定系统前缀放最前，易变后缀（summary/runtime context）放最后。

---

## 3. 目录结构与模块职责

```text
SJTUClaw/
├── claw/                         # Python 主程序
│   ├── agent/                    # Agent Loop、预算、事件、健康监控
│   ├── approval/                 # 高风险工具审批管理
│   ├── channels/                 # 外部渠道（QQ Bot）
│   ├── cli/                      # CLI 入口、REPL、命令解析
│   ├── context/                  # Context Builder、Compact、治理与 Token 预算
│   ├── gateway/                  # FastAPI Gateway、REST API、SSE、上传
│   ├── llm/                      # OpenAI Compatible 客户端与协议适配
│   ├── memory/                   # 长期记忆存储与 Reflection
│   ├── pet/                      # 桌面宠物进程与资源管理
│   ├── prompts/                  # Prompt 模板加载
│   ├── scheduler/                # Cron、Heartbeat、任务分发
│   ├── session/                  # Session/Message 模型、JSONL Store
│   ├── skills/                   # Skill Registry、安装、统计
│   ├── tools/                    # 文件、Shell、网页、Memory、Cron 等工具
│   ├── workspace/                # Workspace 绑定、边界检查、SQLite + CAS 回退
│   ├── config.py                 # 配置加载与运行时入口配置
│   ├── runtime_settings.py       # Web UI 可写设置与敏感配置持久化
│   ├── desktop.py                # Windows 桌面壳
│   ├── paths.py                 # 源码版/PyInstaller版/安装版路径切换
│   ├── main.py                   # 应用主入口
│   └── utils.py                  # 通用工具函数
├── prompts/                      # identity、system prompt、soul、tool contract
├── skills/                       # 内置 Skill 目录
├── webui/                        # React + TypeScript + Vite 前端
├── packaging/windows/            # Windows 安装包构建
├── docs/                         # 配置/测试/打包文档
├── tests/                        # pytest 后端测试
├── pyproject.toml                # Python 项目元数据与 sjtuclaw CLI 入口
├── requirements.txt              # 可复现的 Python 依赖
└── .env.example                  # 环境变量模板
```

### 模块职责矩阵

| 模块 | 职责 | 关键文件 |
|------|------|----------|
| [claw/agent](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/) | Think-Act-Observe 循环、预算、事件流、健康监控 | [loop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py) |
| [claw/approval](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/) | 写/Shell 工具的待审批队列与阻塞等待 | [manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py) |
| [claw/channels](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/) | 外部消息平台适配（QQ Bot WebSocket） | [qq.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/qq.py) |
| [claw/cli](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/) | CLI 入口、REPL、斜杠命令分发 | [commands.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/commands.py) |
| [claw/context](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/) | 上下文组装、Token 预算、压缩、治理 | [builder.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/builder.py) |
| [claw/gateway](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/) | FastAPI 服务、REST API、SSE、中间件 | [server.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/server.py) |
| [claw/llm](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/) | OpenAI 兼容 API 客户端与协议解析 | [client.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/client.py) |
| [claw/memory](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/) | Markdown + YAML 长期记忆与每日 Reflection | [store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/store.py) |
| [claw/pet](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/) | Tkinter 桌面宠物窗口、精灵动画、状态投影 | [app.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/app.py) |
| [claw/prompts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/prompts/) | Prompt 模板加载与最小 Jinja2 渲染 | [__init__.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/prompts/__init__.py) |
| [claw/scheduler](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/) | Cron 服务、Heartbeat、任务分发 | [service.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/service.py) |
| [claw/session](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/) | Session/Message 模型、JSONL 持久化 | [store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) |
| [claw/skills](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/) | Skill 扫描、热重载、安装、使用统计 | [registry.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/registry.py) |
| [claw/tools](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/) | ToolRegistry + 所有内置工具 | [base.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) |
| [claw/workspace](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/) | Workspace 绑定、回退检查点、CAS 对象库 | [rollback.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/rollback.py) |

---

## 4. 核心功能实现详解

### 4.1 统一 Agent Loop

**位置**：[claw/agent/loop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)

这是整个项目的核心——所有用户交互最终都汇入 [run_agent_turn()](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py#L1679)。模块文档字符串明确说明：*"Every entry point (CLI, Gateway, Scheduler, …) must route through this function and must never call LLMClient directly."*

#### 入口函数 `run_agent_turn`

```python
def run_agent_turn(
    session_id: str,
    user_message: str,
    *,
    rollback_manager=None,
    **kwargs,
) -> str
```

**职责**：包装内部 `_run_agent_turn_unlocked`，提供可选的回退/事务语义。
**流程**：
1. 若 `rollback_manager is None`，直接委托给内部循环。
2. 否则获取 `rollback_manager.turn_guard(session_id)`——一个 workspace 锁，覆盖整个回合，防止共享 workspace 的并发会话与检查点或恢复操作交错。
3. 调用 `rollback_manager.create_turn_checkpoint(...)` 创建回合检查点，传入 `partial=bool(kwargs.get("unlimited_mode", False))`。
4. 调用内部循环并注入私有参数 `_rollback_message_id` / `_rollback_checkpoint_id`。

#### 内部实现 `_run_agent_turn_unlocked`

```python
def _run_agent_turn_unlocked(
    session_id, user_message, *,
    session_store, context_builder, tool_registry, llm_client,
    approval_handler=None, media=None, skill_registry=None,
    skill_source="", skill_name="", auto_reason="",
    compaction_worker=None, auto_mode=False, unlimited_mode=False,
    event_callback=None, cancel_event=None, input_event=None,
    _rollback_message_id=None, _rollback_checkpoint_id=None,
) -> str
```

**Think-Act-Observe 主循环**（`while True:`）：

```
┌─────────────────────────────────────────────────────────────────┐
│ Step A: 检查 cancel_event → 返回 status="cancelled"             │
│ Step B: 检查迭代上限 (CLAW_MAX_AGENT_ITERATIONS=15) → 返回 partial │
│ Step C: metrics.record_iteration() + 发出 ThinkingEvent          │
│ Step D: context_builder.build_messages() → llm_client.chat_with_tools()│
│ Step E: 若 response.is_final → _finish_reply(text, status="completed")│
│ Step F: 若 response.is_tool_call:                                  │
│   ├─ 工具上限检查 (CLAW_MAX_TOOL_CALLS_PER_TURN=20)               │
│   ├─ 标准化 native tool_calls + 生成唯一 call_id                 │
│   ├─ 对每个 tool call:                                            │
│   │   ├─ 取消检查 → 标记 batch_cancelled                         │
│   │   ├─ use_skill 特殊路径 → _handle_skill_select()             │
│   │   ├─ 审批门：approval_handler + AUTO/UNLIMITED 规则          │
│   │   ├─ 执行：tool_registry.execute_by_name(name, args)         │
│   │   ├─ 记录结果（_record_outcome）                              │
│   │   ├─ 卡循环检测（call_signature + result fingerprint）         │
│   │   └─ 发出 ToolCallEndEvent                                    │
│   ├─ 注入 pending_skill_injections                                │
│   └─ 保存会话 → continue 循环                                     │
│ Step G: 都不是 → ErrorEvent + _finish_reply(status="partial")    │
└─────────────────────────────────────────────────────────────────┘
```

#### 单一出口 `_finish_reply`

`_finish_reply(text, *, empty_reason="", status="completed")` 是所有路径的唯一出口，保证：
- 恰好一条非空 assistant 回复被持久化（用 `reply_finished` nonlocal 标志防止重复）。
- 完成状态为空时降级为 `partial` 或 `failed`。
- 非完成状态时附加 `_completion_brief(...)` 任务简报。
- 持久化到 Session、发出 `FinalEvent`、更新 metrics 与 health monitor。
- 当 `_MAX_METRIC_SESSIONS`（500）超过时驱逐最旧会话。

#### 多层防御策略

| 防御层 | 机制 | 默认阈值 |
|--------|------|----------|
| 迭代上限 | `turn_count > _MAX_AGENT_ITERATIONS` | 15 |
| 每回合工具上限 | `tool_calls_used >= _MAX_TOOL_CALLS_PER_TURN` | 20 |
| 卡循环检测 | `stagnant_count >= _MAX_IDENTICAL_TOOL_CALLS`（同 tool+args+result） | 3 |
| 拒绝上限 | `_rejection_tracker[key] >= _MAX_REJECTIONS_PER_OPERATION` | 3 |
| 取消 | `cancel_event.is_set()` | 外部触发 |
| 审批 fail-closed | `needs_approval and approval_handler is None` | 始终 |
| UNLIMITED 强制审批 | `force_approval = unlimited_mode` | 始终 |

#### 事件流时间线（成功回合，一个工具调用）

```
ThinkingEvent(iteration=1)
  → ToolCallStartEvent(call_id, tool_name, args, iteration=1)
    → ToolCallEndEvent(call_id, tool_name, ok=True, result, duration_ms)
ThinkingEvent(iteration=2)
  → FinalEvent(content=final_text)
```

#### Skill 注入协议

`use_skill` 工具具有独特的 `skill_select` 安全级别：

1. **显式调用**（`/skill name task`）：Skill 内容在循环开始前作为用户消息注入，`skill_already_injected_for_turn = True`。
2. **模型自选调用**：模型作为工具调用 `use_skill` → 路由到 `_handle_skill_select` → 暂停等待用户审批 → 通过后构造注入消息 → 调用方先追加 tool 结果，再追加注入消息（保持原生 function-calling 协议）。

**关键安全属性**：当模型调用 `use_skill` 时，Agent Loop 在用户确认**之前**暂停——模型只在用户批准后才看到完整 Skill 指令。

### 4.2 上下文构建与压缩

**位置**：[claw/context/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/)

#### ContextBuilder —— 唯一的 LLM 消息组装器

[builder.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/builder.py) 是组装 LLM `messages` 数组的**唯一**位置。模块文档明确："CLI code and the LLM client must never build this array directly."

`build_messages(...)` 方法按以下顺序组装：

1. **稳定系统前缀**（prompt-cache 友好，缓存）：
   - Identity 块（runtime、workspace_path、platform_policy、channel、timezone）
   - System prompt（来自 `prompts/system_prompt.md`）
   - Soul（来自 `prompts/soul.md`，如已自定义）
   - Tool contract（来自 `prompts/tool_contract.md`）
   - Bootstrap 块（从 workspace 根加载 `AGENTS.md`/`SOUL.md`/`USER.md`）

2. **Memory 块**：包含分类计数、Top 5 最近更新条目预览，以及调用 `recall`/`remember` 的规则。按 `MemoryStore.version` 缓存失效。

3. **Tool definitions**：原生 function calling 通过 API `tools` 参数传递（不是文本）。

4. **Skill 块**：轻量 Skills 索引 + 摘要 + 已加载 always-on skills。

5. **Session summary**：包装 `session.summary`（或覆盖），带 reference-only 前缀。

6. **会话消息**：迭代 `session.get_unconsolidated_messages()`：
   - 从 dict 中剥离 `media` key
   - 孤儿 tool 消息降级为 assistant 内容前缀 `[历史工具结果]`
   - **最后一条用户消息**追加 runtime context，保持 user-content 前缀稳定（prompt-cache 友好）
   - 多模态内容通过 `_multimodal_user_content` 构建

7. **Provider 规范化**：`_merge_leading_system_messages` 将前导 system 消息合并为一条（适配 SJTU Qwen/LiteLLM 路由拒绝多个连续 system 消息）。

8. **预算**：若 `return_budget=True`，返回 `(messages, ContextBudget)` 元组，默认上限 25,600 tokens。

#### ContextBudget —— 不可变快照

[budget.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/budget.py) 的 `ContextBudget.measure(...)` 计算：
- `fixed_overhead_tokens` = system_prompt + soul + memory + tools + skills + summary
- `total_tokens` = fixed_overhead + messages
- `available_tokens` = max_tokens - total
- `usage_ratio` = total / max
- `check_overflow()` 在 `usage_ratio >= 1.05` 时抛出 `ContextOverflowError`

#### Compaction —— 不删除原始消息

[compaction.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction.py) 的核心函数：

- `needs_compaction(session, ...)`：两个独立触发条件——消息 token 阈值或上下文预算压力，都受 `KEEP_RECENT_MESSAGES_MIN=4` 门控。
- `compact_session(session, llm_client, ...)`：**纯函数**——不修改 session，返回 `CompactionResult`。算法：
  1. 在 `session.get_unconsolidated_messages()` 上操作
  2. 计算 split index，clamped 到 `len - keep_min`
  3. 通过 `_build_compaction_request` 构建压缩请求（工具输出 >500 字符预修剪为占位符）
  4. 调用 `llm_client.chat(request_messages)`
  5. 拒绝空摘要
  6. 返回 `CompactionResult`
- `apply_compaction_result(session, result)`：**唯一的修改器**——推进 `session.last_consolidated` 投影边界，替换 `session.summary`，调用 `session.touch()`。**原始 transcript 完全保留**，支持回退和审计。

#### CompactionWorker —— 后台线程

[compaction_worker.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction_worker.py)：

- `submit(session)`：在 brief lock 下快照消息+摘要+revision，然后在锁外执行慢速 LLM 调用。**Revision 守卫**：仅当 `session.revision == snapshot_revision` 时才应用结果——防止陈旧摘要覆盖回退后的状态。
- `start_idle_compaction()`：启动 120s 轮询的后台空闲会话自动压缩循环。
- 单次只允许一个 compaction，并发提交被静默丢弃。

#### ContextGovernor —— 8 步治理管道

[governance.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/governance.py) 的 `ContextGovernor.prepare_for_model(...)` 应用 8 步管道（按顺序）：
1. 剥离占位符 assistant 消息
2. 剥离格式错误的 tool_calls
3. 丢弃孤儿 tool 结果（无匹配 assistant tool_call）
4. 回填缺失的 tool 结果
5. 应用 tool 结果预算（截断超长结果，加 `[truncated]`）
6. 紧凑 in-flight 溢出（摘要大 tool 结果，记录 `compacted_tool_call_ids`）
7. 仍溢出时从前部 snip 历史（锚定至少一条用户消息）
8. 再次去孤儿 / 回填（snip 可能破坏配对）

**关键设计**：Copy-on-write——每个方法在无变更时返回原列表，否则返回新列表。`compacted_tool_call_ids` 集合跨调用保留状态。

#### TokenCounter —— CJK 友好

[token_counter.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/token_counter.py)：
- 懒加载 `tiktoken`，使用 `o200k_base` 编码
- **回退启发式**：`cjk_count * 2 + max(1, other_chars // 4)`——CJK 字符 ×2 token，其他 4 字符/token
- `count_tokens_for_messages(messages)` 只统计 content（不含 role），匹配旧 `MAX_CHARS_BEFORE_COMPACTION` 语义

### 4.3 工具系统与安全审批

**位置**：[claw/tools/base.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) + [claw/approval/manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py)

#### Tool 数据结构

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema
    handler: Callable[[dict[str, Any]], ToolResult]
    safety_level: str = "read_only"  # read_only | write | shell | download
    concurrency_safe: bool = False
    max_result_chars: int = 0  # 0 = 不限
```

`ToolResult` 强制不变量：成功必无 error，失败必无 content。

#### ToolRegistry —— 注册与执行

[base.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) 的 `ToolRegistry`：
- `register(tool)`：验证 name 模式 `[A-Za-z_][A-Za-z0-9_-]{0,63}`，handler 可调用，schema 结构，拒绝冲突。
- `execute_by_name(name, args, *, max_result_chars=0)`：
  1. 运行 `prepare_call` 钩子（防御性 copy args）
  2. 通过 `_validate_args` 验证 args（支持 required/type/enum/string length/number range/array items）
  3. 在 try/except 中执行 handler，**永不抛出**
  4. 验证 result 类型
  5. 超长 content/error 自动截断
- `set_context(ctx)`：向所有 `ContextAware` 工具传播 `RequestContext`

#### ToolGuardrails —— 每回合调用上限

```python
class ToolGuardrails:
    def __init__(self, *, max_calls_per_tool=50, max_total_calls=200)
    def check(self, tool_name) -> str | None  # 返回错误消息或 None
```

#### SSRF / Workspace 边界分类

`classify_boundary_error(tool_name, error_text, *, violation_counts=None)` 返回 `(升级消息 | None, is_ssrf)`：
- SSRF 违规立即升级，附加禁止绕过的中文边界注释
- Workspace 违规**累计 3 次**后才升级（容忍偶发误报）

#### ApprovalManager —— 高风险工具审批

[manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py)：

```python
class ApprovalManager:
    def create(session_id, tool_name, tool_args) -> ApprovalRequest
    def approve(approval_id) -> ApprovalRequest | None  # 幂等
    def reject(approval_id, reason="") -> ApprovalRequest | None
    def wait(approval_id, timeout=300.0) -> ApprovalRequest | None
        # 超时自动拒绝（reason="审批超时，自动拒绝"）
```

**线程模型**：每个 approval 一个 `threading.Event`，`wait()` 阻塞直到 `approve()`/`reject()` 设置 Event。Agent Loop 在工具执行前调用 `wait()` 阻塞。

**清理策略**：完成的审批保留 10 分钟 + FIFO 上限 200 条。

#### 内置工具一览

| 工具名 | 文件 | safety_level | 说明 |
|--------|------|--------------|------|
| `current_time` | [readonly.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/readonly.py) | read_only | ISO-8601 时间，支持 tz |
| `list_dir` | readonly.py | read_only | 目录列表，concurrency_safe |
| `read_file` | readonly.py | read_only | 64 KiB 上限，UTF-8 |
| `create_file` | [update.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/update.py) | write | 文件不存在时创建 |
| `overwrite_file` | update.py | write | 覆盖写入 |
| `edit_file` | update.py | write | 单次匹配替换（0 或 >1 匹配失败） |
| `new_shell` | [shell.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/shell.py) | shell | 创建持久 cwd 的 shell 会话 |
| `run_command` | shell.py | shell | 执行命令，跟踪 cwd |
| `web_search` | [web.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/web.py) | network | Tavily → DuckDuckGo → Bing |
| `web_fetch` | web.py | network | SSRF 安全的页面抓取 |
| `remember` | [memory_tools.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/memory_tools.py) | write | 保存长期记忆 |
| `recall` | memory_tools.py | read_only | 关键词 + CJK 字符召回 |
| `skills_list` | [skills_tool.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/skills_tool.py) | read_only | Skill 索引 |
| `skill_view` | skills_tool.py | read_only | Skill 详情（渐进式披露） |
| `skill_manage` | [skill_manager_tool.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/skill_manager_tool.py) | write | LLM 驱动的 Skill 增删改 |
| `cron` | [cron_tool.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/cron_tool.py) | read_only | add/list/remove 定时任务 |
| `create_download` | [download.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/download.py) | download | 注册下载 |
| `copy_attachment_to_workspace` | [attachment.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/attachment.py) | write | 复制附件到 workspace |

#### 注册编排

[tools/__init__.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/__init__.py) 的 `register_all_tools(...)` 是核心工厂：
- 始终注册 readonly + web 工具
- 条件注册 skill/memory/cron 工具
- 当 `workspace_manager` + `session_id_provider` 都提供时，注册所有 workspace 感知工具

### 4.4 Workspace 沙箱与回退系统

**位置**：[claw/workspace/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/)

#### WorkspaceManager —— 路径沙箱

[manager.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/manager.py) 的 `resolve(session_id, path_str, *, must_exist=False)`：

1. 原子读取 unlimited + workspace 状态（避免 TOCTOU）
2. unlimited 模式：解析 raw path
3. 无 workspace 绑定：抛 `WorkspaceError`
4. **拒绝绝对路径**——只允许相对路径
5. `(ws / p).resolve()` + `relative_to(ws)` 检查
6. `must_exist=True` 时验证存在

持久化到 `data/workspace/bindings.json`，使用 PID+UUID 的唯一 tmp 文件防止跨 manager 冲突，`_atomic_replace_with_retry` 重试 7 次（Windows 防 AV/索引锁定）。

#### UNLIMITED 模式

`set_unlimited(session_id, unlimited)` —— 添加/移除 `_unlimited_sessions`，绕过所有 workspace 边界检查。`require(session_id)` 在 unlimited 模式返回文件系统根（`C:\` 或 `/`）。

#### WorkspaceRollbackManager —— 检查点 + CAS 对象库

[rollback.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/rollback.py)：

**存储架构**：
- `objects_dir`（CAS 对象库）：`objects/xx/yyyy...`（Git 风格分片，SHA-256 哈希）
- `state.db`（SQLite WAL）：3 张表
  - `bindings`：session_id, binding_id, root_path, generation, enabled
  - `checkpoints`：checkpoint_id, session_id, binding_id, parent_checkpoint_id, target_message_id, manifest_json, session_json, kind, status, partial
  - `operations`：operation_id, session_id, target_checkpoint_id, safety_checkpoint_id, status, error

**捕获流程** `create_turn_checkpoint(...)`：
1. 扫描 workspace（递归，跳过 `.git`/`.hg`/`.svn`/`.sjtuclaw-rollback-tmp`，**拒绝扫描 storage_root 自身**）
2. 存储文件 blob 到 CAS（hash + 重验证 + 原子 replace）
3. zlib + base64 压缩 session 快照
4. 插入 SQLite checkpoint 行
5. **使前一个 undo 安全点失效**（单步 undo）

**回退执行** `rollback(session_id, target=None)`：
1. 解析目标 checkpoint，解码快照
2. 插入 **safety checkpoint** 捕获当前状态
3. 创建 `operations` 行 `status="PREPARED"`
4. 应用 manifest 修改文件
5. 更新到 `FILES_APPLIED`
6. 恢复 session，`fsync=True` 保存
7. **异常时**：读取 safety checkpoint，补偿重放 safety manifest，标记 `COMPENSATED`
8. 成功时：标记 `COMMITTED`，旧 turn checkpoints 标记 `orphaned`

**崩溃恢复** `recover_incomplete_operations()` 在启动时运行，幂等补偿被进程退出中断的回退操作。

**GC**：mark-and-sweep 扫描 `objects_dir`，读取所有 manifest 计算引用哈希，删除未引用 blob。

### 4.5 会话持久化与崩溃恢复

**位置**：[claw/session/store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) + [models.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/models.py)

#### JSONL 格式

每个 session 是 `<base64-encoded-session_id>.jsonl` 文件：
- 第 1 行：metadata（`_type: "metadata"`, session_id, title, summary, last_consolidated, revision, metadata）
- 后续行：每条消息一个 JSON 对象

**优势**：损坏隔离（单行损坏不影响其他行）、流式追加、原子写入。

#### Session/Message 模型

[models.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/models.py)：

`Message` 字段：role, content, message_id（稳定 UUID）, rollback_checkpoint_id, tool_calls, tool_call_id, name, timestamp, _command, media, injected_event, subagent_task_id, latency_ms。

`Session` 关键方法：
- `get_unconsolidated_messages()` → `messages[last_consolidated:]`
- `get_history(max_messages, max_tokens, extend_to_user)` → 清洗后的历史（过滤 _command 消息、剥离 runtime context、合成 image breadcrumbs、token 预算截断对齐到 user turn）
- `retain_recent_legal_suffix(max_messages, extend_to_user)` → 保留合法最近后缀
- `to_snapshot_dict()` → 无损内部快照（用于回退）
- `touch()` → 更新 updated_at + 递增 revision

#### 崩溃恢复

```python
set_runtime_checkpoint(session, payload)  # 持久化进行中回合状态到 metadata
mark_pending_user_turn(session)
restore_runtime_checkpoint(session) -> bool  # 物化被中断的回合到历史
restore_pending_user_turn(session) -> bool  # 关闭用户消息已持久化但回复未生成的回合
```

`restore_runtime_checkpoint` 重构：assistant 消息 + 已完成 tool 结果 + 待执行 tool 调用（带 error 内容），用 `_msg_key` 去重。

#### 会话分叉

`fork_session_before_user_index(source_key, target_key, before_user_index)` → 从 source_key 创建 target_key，在第 N 条用户消息前截断。深拷贝 metadata，剥离 volatile keys（goal_state, pending_user_turn, runtime_checkpoint, title 等）。

### 4.6 长期记忆与每日 Reflection

**位置**：[claw/memory/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/)

#### MemoryStore —— 文件系统即数据库

[store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/store.py)：

每条记忆是一个独立 `.md` 文件，位于 `data/memory/<category>/`：
- YAML frontmatter：id, category, tags, importance, source_session_id, created_at, updated_at, last_recalled_at, recall_count
- Markdown body：富文本内容

**Categories**：`user_preference`, `project`, `decision`, `fact`, `general`

**召回算法** `recall(query, category=None, limit=5)`：
1. 拆分查询为 terms（≥2 字符）+ 提取 CJK 字符
2. **评分**：
   - 标签匹配：+10 完全匹配，+5 term 匹配
   - 内容匹配：+8 完全 query 子串，+3 per matching term
   - CJK 字符匹配：`+min(matched/total * 6.0, 6.0)`（无基础分时）
   - **Boosts**（仅当有基础匹配）：user_preference +2, importance +importance, 7 天内创建 +1, recall_count `+min(n*0.5, 3.0)`, 1 小时内召回 +2 / 24 小时内 +1
3. 降序排序，取 top `limit`（clamped [1, 20]）
4. **召回追踪**：更新 `last_recalled_at` + 递增 `recall_count`，写回磁盘（失败静默）

`version` 属性在每次变更时递增，用于 `ContextBuilder` 失效 memory 块缓存。

#### ReflectionManager —— 每日自动总结

[reflection.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/reflection.py)：

后台轮询线程（60s 间隔），每天在配置时间（默认 23:00）触发一次：
1. `_gather_sessions()` → 收集自上次运行以来有消息变更的会话快照（最后 ~20 条消息，每条截断 300 字符）
2. `_extract_facts_batch(sessions)` → 构建包含"## 已有记忆（避免重复提取）"块的单条用户消息，调用 LLM 提取长期有价值的记忆
3. `_parse_facts_from_response(raw)` → 剥离 markdown 代码围栏，解析 JSON 数组，验证 category/content/tags/importance（1-5）
4. `_save_facts(facts)` → 通过 `memory_store.add(...)` 保存，source_session_id="reflection"

**配置持久化**：`reflection_config.json`（enabled, time, last_run_at, run_history 上限 50 条）。

### 4.7 Skill 系统

**位置**：[claw/skills/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/)

#### SkillRegistry v7 —— 扫描 + 热重载

[registry.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/registry.py)：

`SkillInfo` 字段：name, description, instructions, directory, source, assets, references, always, disabled, requires_bins, requires_env, available, missing_deps。

**扫描** `_scan()`：遍历 skills 目录（flat + 一层 category 嵌套），解析每个 `SKILL.md` 的 YAML frontmatter：
- 验证 name 匹配 regex `^[a-z0-9][a-z0-9._-]{0,63}$`
- 提取 `always` 标志（支持 bool 或 "1"/"true"/"yes"/"on"）
- 提取 `requires.bins` / `requires.env` 列表
- 通过 `_check_requirements` 检查 `shutil.which` / `os.environ.get`
- 收集 `assets/` 和 `references/` 子目录文件

**热重载** `rescan(*, force=False)`：
- `force=True`：全量重扫，返回所有当前名为 added/removed/modified
- 否则：peek `SKILL.md` mtimes（只重新解析 name 用于廉价 keying），计算 diff，只重新解析受影响 skills
- 返回 `{"added": [...], "removed": [...], "modified": [...]}` 用于精确缓存失效

`version` 属性在每次扫描时递增。

#### SkillUsageStore —— 使用统计

[usage.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/usage.py)：

Sidecar JSON store（`.usage.json`），`FileLock` 跨进程安全。
- `bump_use(name)` / `bump_view(name)` / `bump_patch(name)` → 计数器递增 + 时间戳
- `apply_automatic_transitions(known_skills, *, stale_after_days=30, archive_after_days=90)` → 自动状态转换 active → stale → archived（pinned 跳过）
- `forget(name)` → 删除使用数据（Skill 删除时调用）

#### management.py —— 安全的 Skill 包安装

[management.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/management.py)：

**严格验证**：
- `MAX_PACKAGE_BYTES = 20 MiB`
- `MAX_TOTAL_UNPACKED_BYTES = 50 MiB`
- `MAX_FILE_COUNT = 500`
- `MAX_FILE_BYTES = 5 MiB`
- `ALLOWED_SUFFIXES`：代码/数据/图片/配置格式白名单
- `ALLOWED_TOP_LEVEL_FILES = {"SKILL.md", "README.md", "LICENSE", "LICENSE.md"}`
- `ALLOWED_DIRS = {"assets", "references", "templates"}`
- 拒绝 symlinks/hardlinks/设备文件
- 路径遍历防御（`_ensure_child_path`）

`install_skill_package_bytes(data, filename, *, replace=False)` → 验证 → 提取到 tmp → 重新验证 → 移动到最终位置。

### 4.8 定时任务与 Heartbeat

**位置**：[claw/scheduler/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/)

#### CronService —— 持久化 + 计时器循环

[service.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/service.py)：

**CronSchedule 类型**：
- `kind: "at"` → 一次性，`at_ms` 时间戳
- `kind: "every"` → 间隔，`every_ms` 毫秒
- `kind: "cron"` → cron 表达式，可选 `tz`

**CronPayload**：`kind: "system_event" | "agent_turn"`, message, session_key, origin_channel, origin_chat_id, origin_metadata, depends_on（v2 依赖注入）。

**CronJobState**：next_run_at_ms, last_run_at_ms, last_status, run_history（上限 20）, paused_at_ms, paused_reason, run_claim, fire_claim（v2 at-most-once）。

**生命周期**：
- `start(loop=None)` → 若提供 loop（如 FastAPI lifespan）在该 loop 运行，否则 spawn 后台 daemon 线程 "claw-cron"
- `_start_on_loop()` → 加载 store，拒绝启动若损坏（保留 `.corrupt-<ts>` 备份），重新计算 next runs，保存，arm timer
- `stop()` → 取消 timer，stop loop，join 线程 5s 超时

**存储**：JSON 文件 + `FileLock` 跨进程安全 + `action.jsonl` 离线动作追加 + `runs/` 输出目录。

**计时器** `_arm_timer()`：
- 线程安全，检测从 cron loop 还是外部线程调用，使用 `call_soon_threadsafe`
- `_get_next_wake_ms()` → 最早 next_run_at_ms

**执行** `_on_timer()`：
1. 记录 heartbeat
2. 找出到期 jobs
3. 跳过有 active run_claim 的 jobs（at-most-once）
4. **先推进 recurring jobs 的 next_run**（崩溃安全）
5. 执行 jobs
6. 记录 success heartbeat

**`_execute_job(job)`**：
1. 注入依赖上下文（`depends_on` jobs 的输出）
2. 一次性 jobs 标记 `run_claim`
3. 调用 `self.on_job(job)` 回调
4. 处理 `CronJobSkippedError` / `CancelledError` / generic `Exception`
5. 恢复原始 message
6. 清除 run_claim
7. 保存执行输出到 `runs/<job_id>/<timestamp>.md`（每 job 保留 50 个）
8. 追加 `CronRunRecord` 到历史
9. 处理 repeat 限制（`repeat_times` 后自动删除）
10. 一次性 "at" jobs：删除或禁用；recurring：计算 next run

**公共 API**：`add_job`, `register_system_job`, `remove_job`（拒绝 system_event 保护）, `enable_job`, `update_job`, `run_job`（手动触发）, `pause_job`, `resume_job`, `trigger_job`（立即执行）, `status`。

#### dispatcher.py —— 路由到 Agent Loop

[dispatcher.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/dispatcher.py) 的 `create_cron_dispatcher(...)` 工厂创建一个 `async def dispatch(job)` 闭包：
1. Heartbeat 处理 → 委托给 `on_heartbeat` 钩子
2. agent_turn jobs：
   - 解析 session（不在磁盘时尝试 `on_session_resolve` 钩子，仍缺失用最近 session，最后才创建）
   - 调用 `on_turn_active` / `on_turn_start` 钩子
   - `_run_bound_turn()` 内部函数：设置 thread-local session_id → 更新 cron context → 获取 `cron_token`（嵌套 cron 检测）→ 调用 `run_agent_turn(..., input_event="cron_trigger")` → finally 重置 cron context
   - 在线程中运行（`asyncio.to_thread`）因为 agent loop 是同步的
   - finally 调用 `on_turn_idle` / `on_turn_finish`
   - 通过 `on_deliver` 投递回复到 origin_channel

#### callbacks.py —— Heartbeat 系统任务

[callbacks.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/callbacks.py) 的 `HeartbeatCallback`：
1. 读取 `HEARTBEAT.md`，文件缺失返回 None
2. 通过 `_has_active_tasks(content)` 检查是否有活跃任务（解析 `## active tasks` section，跳过 HTML 注释）
3. 构造 prompt = `_HEARTBEAT_PREAMBLE + content`，调用 `run_agent_turn(default_session_key="heartbeat")`
4. 修剪 heartbeat session 历史到 `keep_recent_messages`（默认 8）
5. 仅当回复不是 "All clear." 时返回，否则返回 None

`make_heartbeat_system_job(heartbeat_cfg)` 构造系统 job：id="heartbeat", every_ms=interval_s*1000, kind="system_event"。

### 4.9 Gateway REST API 与 SSE

**位置**：[claw/gateway/server.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/server.py)

#### FastAPI 应用

版本 0.3.0，中间件栈：
1. `CORSMiddleware`
2. `RequestSizeMiddleware`（10 MB 默认 / 50 MB 附件）
3. `RateLimitMiddleware`（300 req/60s + burst 10）
4. `GatewaySecurityMiddleware`（origin + token 检查）
5. `RequestLoggingMiddleware`（request_id + 慢请求告警）

#### RuntimeLLMClient —— 可变 LLM 客户端

允许 WebUI 在 LLM 未配置时启动（"settings mode"）。`set_config(config)` 创建新 `LLMClient`，`clear(message=None)` 重置为未配置状态。

#### 主要 REST 端点

| 分组 | 端点 | 方法 | 说明 |
|------|------|------|------|
| Chat | `/chat` | POST | 阻塞式 agent turn |
| Chat | `/chat/stream` | POST | SSE 流式 |
| Chat | `/stop` | POST | 取消 active turn |
| Chat | `/command` | POST | 斜杠命令 |
| Sessions | `/sessions` | GET/POST | 列表/创建 |
| Sessions | `/sessions/{id}/messages` | GET | 完整线程 + 标志 |
| Sessions | `/sessions/{id}` | PATCH/DELETE | 重命名/删除 |
| Attachments | `/sessions/{id}/attachments` | POST/GET | 上传/列表 |
| Workspace | `/workspace` | GET/POST/DELETE | 绑定管理 |
| Workspace | `/workspace/pick` | POST | 原生文件夹选择器 |
| Rollback | `/sessions/{id}/rollback` | GET/POST | 状态/预览/应用 |
| Approvals | `/approvals` | GET | 待审批列表 |
| Approvals | `/approvals/{id}/approve\|reject` | POST | 决策 |
| Pet | `/pet/*` | various | 宠物设置/资源/窗口 |
| Downloads | `/downloads`, `/downloads/{id}` | GET | 下载列表/服务 |
| Skills | `/skills` | GET/POST/DELETE | 列表/上传/删除 |
| Admin | `/admin/system-prompt`, `/admin/soul` | GET/PUT | Prompt 编辑 |
| Memory | `/memories` | GET/POST/PATCH/DELETE | CRUD |
| Reflection | `/reflect/*` | GET/POST | 配置/手动运行 |
| Cron | `/cron/jobs/*` | various | CRUD |
| Settings | `/settings/llm`, `/settings/ui/avatar`, `/settings/channel/*` | GET/PUT | 运行时设置 |
| QQ | `/qq/status`, `/settings/channel/qq/onboard/*` | various | QQ 状态/QR 登录 |

#### SSE 流式端点 `/chat/stream`

`POST /chat/stream` 实现：
1. 创建 `queue.Queue` 和 `threading.Event` done 信号
2. Spawn daemon 线程运行 `run_agent_turn`，`event_callback` 入队事件，包括最终 `_session_info` 和自动标题 `_title` 事件
3. 异常时入队 `ErrorEvent` + 兜底 `FinalEvent`
4. `_event_generator` async 生成器：`loop.run_in_executor` 0.1s 超时轮询队列，空时发 `: keepalive\n\n` 注释防代理超时
5. 返回 `StreamingResponse`，`text/event-stream` + `X-Accel-Buffering: no` 禁用 nginx 缓冲

`_event_to_sse(event)` 将 `TurnEvent` 序列化为 `data:` SSE 行，丢弃空字段。

#### Web UI 挂载

`_WEB_DIR` 存在时，挂载 `StaticFiles(directory=str(_WEB_DIR), html=True)` 在 `/` 作为 SPA catch-all fallback。**最后挂载**，确保 API 路由优先。

#### Lifespan 处理器

`_lifespan(_app)`：
- 启动：`_cron_service`, `_reflection_mgr`, `_compaction_worker.idle_compaction`
- 可选启动桌面宠物（Windows 或 DISPLAY 设置，`PYTEST_CURRENT_TEST` 下跳过）
- 启动 QQ channel（`QQ_ENABLED=true` 且 AppID/Secret 存在）
- 关闭：停止 QQ channel + 取消 task，停止 compaction, reflection, cron, pet

### 4.10 QQ Bot 渠道

**位置**：[claw/channels/qq.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/qq.py)

#### QQChannel —— WebSocket + REST 适配器

继承 `BaseChannel`，连接官方 QQ Bot WebSocket Gateway + REST API（`api.sgroup.qq.com`）。

**连接状态**：aiohttp WebSocket（fresh per-reconnect）, httpx.AsyncClient（懒创建）, token cache + asyncio.Lock（double-checked locking）。

**_listen_loop()** —— 持久外层循环：
- 记录 connect_time，调用 `_connect_and_listen()`
- 成功重置 backoff 和 quick_disconnect_count
- `QQCloseError` 处理：
  - **快速断开检测**：连接 < 5s 且 3 次 → "Check AppID/Secret" 并返回
  - **致命 codes** {4001, 4002, 4010-4014, 4914, 4915}：停止重连
  - **限流** 4008：sleep 60s
  - **Token 无效** 4004：清除 token cache
  - **Session 无效** {4006, 4007, 4900-4913}：清除 session_id 和 last_seq（强制 fresh Identify）
  - **4009 超时**：**不清除**（可恢复）
  - 指数退避 [2, 5, 10, 30, 60]s，最多 100 次重连

**Token 管理** `_ensure_token()`：double-checked locking + 60s 预过期 buffer。POST 到 `bots.qq.com/app/getAppAccessToken` with `{appId, clientSecret}`。

**WebSocket 层**：
- `_open_ws(gateway_url)`：fresh aiohttp session，**代理支持**（`WSS_PROXY`/`wss_proxy`/`HTTPS_PROXY`/`https_proxy`/`ALL_PROXY`/`all_proxy` 顺序）
- `_heartbeat_loop()`：sleep `_heartbeat_interval`（server interval × 0.8，主动 20% 加速）发送 `{"op": 1, "d": last_seq}`
- `_send_identify()`：intents `(1<<25) | (1<<12) | (1<<26)`（C2C_GROUP_AT_MESSAGES, DIRECT_MESSAGE, INTERACTION），shard [0,1]，properties identify "SJTUClaw"
- `_send_resume()`：op 6 with session_id 和 seq

**Payload 分发** `_dispatch_payload(payload)`：
- op 10 Hello → 读 heartbeat_interval，有 session_id+last_seq 则 resume，否则 identify
- op 0 Dispatch → 更新 last_seq，路由 READY/C2C_MESSAGE_CREATE/GROUP_AT_MESSAGE_CREATE/DIRECT_MESSAGE_CREATE/INTERACTION_CREATE
- op 11 Heartbeat ACK → no-op
- op 7 Server reconnect → 关闭 WS
- op 9 Invalid Session → truthy 则可恢复，否则清除 session_id/last_seq

**消息处理**：
- `_is_duplicate(msg_id)` → 300s 窗口 + 1000 条上限的 dedup
- `_handle_c2c`/`_handle_group`/`_handle_dm` → 权限检查 → `_handle_message` → 回复

**发送** `send(msg)`：
- media 优先：`_send_media`（URL 或本地文件，9 MB 上限，base64 编码，POST `/v2/{groups|users}/{chat_id}/files` 然后 POST media message）
- text：截断 4000 字符，POST `/v2/{groups|users}/{chat_id}/messages`

**审批流** `send_approval(...)`：序列化 tool_args（截断 900 字符），构建中文消息 + inline keyboard + 斜杠命令 fallback。

#### qq_interactions.py —— 内联审批按钮

`build_approval_keyboard(approval_id)` 构建 QQ inline-keyboard JSON：
- 单行两按钮："✅ 允许"（approve, style 1）和 "❌ 拒绝"（reject, style 0）
- `permission: {type: 2}`（指定用户）
- `click_limit: 1`
- `visited_label` 显示点击后文本

`parse_interaction(raw)` 解析 `INTERACTION_CREATE` payload，`_on_interaction` ACK 交互（PUT `/interactions/{id}`）然后调用 `_interaction_handler`。

#### qq_onboard.py —— QR 扫码登录

`qr_register(timeout_seconds=600)` 流程：
1. `_create_bind_task()` → POST `/lite/create_bind_task` with `{key: aes_key}` → 返回 `(task_id, aes_key)`
2. `_render_qr(url)` → ASCII QR 打印（Windows GBK stdout workaround：包裹 UTF-8 TextIOWrapper，detach 防止 `__del__` 关闭底层 buffer）
3. 轮询 `_poll_bind_result(task_id)` 每 2s
4. COMPLETED → `decrypt_secret(encrypted_secret, aes_key)`（AES-256-GCM，IV 12 bytes + ciphertext + tag 16 bytes）→ 返回 `{app_id, client_secret, user_openid}`
5. EXPIRED → 最多 3 次刷新

#### qq_crypto.py —— AES-256-GCM

`generate_bind_key()` → `os.urandom(32)` base64 编码
`decrypt_secret(encrypted_base64, key_base64)` → 懒加载 `AESGCM`，base64 解码，拆分 IV（12 bytes）+ ciphertext_with_tag，`aesgcm.decrypt(iv, ciphertext_with_tag, None)`

**安全属性**：只有 CLI 持有 AES key，即使 `poll_bind_result` 响应被截获，加密的 `bot_encrypt_secret` 也无法解密。

### 4.11 桌面宠物

**位置**：[claw/pet/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/)

#### DesktopPet —— Tkinter 窗口

[app.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/app.py) 的 `DesktopPet` 类：

**精灵图集**：192×208 cells × 8 cols × 9-11 rows。LANCZOS 缩放 + `_make_color_key_safe`（alpha binarization，去除 Windows color-key 透明度的暗边）。

**动画系统** `ANIMATIONS` dict：`idle`, `running-right`, `running-left`, `waving`, `jumping`, `failed`, `waiting`, `running`, `review`。

**窗口**：`overrideredirect(True)` + topmost + `TRANSPARENT_COLOR = "#010203"` color-key 透明度。

**DPI 感知**：`SetProcessDpiAwarenessContext(-4)`（PER_MONITOR_AWARE_V2）。

**交互**：
- 拖动：`<ButtonPress-1>` + `<B1-Motion>` + `<ButtonRelease-1>`，移动窗口 `geometry(+x+y)`，启动 `running-right`/`running-left` 动画
- 双击：`<Double-1>` → 打开输入 popup（取消 pending playful reply）
- 右键：`<Button-3>` → 菜单（审批 approve/reject + 关闭宠物）
- 悬停：`<Motion>` → `jumping` 动画
- 单击释放：400ms 后 `_show_playful_reply`（从 36 条俏皮回复中随机选一条，可被双击取消）

**输入 popup**：Toplevel 透明背景 + 圆角矩形 Canvas + tk.Entry + 橙色圆形发送按钮。支持 Return 发送，`<<Paste>>` 拦截剪贴板图片（`ImageGrab.grabclipboard()`），`<FocusOut>`/`<Escape>` 关闭。

**GatewayClient**：HTTP 客户端封装，`get_state`, `approve`, `reject`, `save_position`, `notify_closed`, `create_session`, `fetch_sessions`, `send_message`（180s 超时）, `upload_image`（手写 multipart/form-data + `X-SJTUClaw-Internal: desktop-pet` header）。

**轮询**：`_poll_gateway()` 后台线程每 1s `GET /pet/state` → `_updates` queue → `_drain_updates()` 每 100ms 在 Tk 主线程处理。检测 pet selection 变化时 `close(notify=False)`。

**气泡**：`_refresh_bubble()` 优先级：local message（未过期）> approval pending > remote state message。200 字符上限，`_update_bubble` 用 `font.measure` 自适应宽度，clamped top ≥2px 防角落裁剪。

#### PetCatalog —— 资源管理

[catalog.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/catalog.py)：

- `PetSettings`：enabled, selected_pet_id="yuexinmiao", launch_on_gateway_start, position_x/y
- 用户 pets 优先，bundled 次之，按 id 去重
- `install(...)`：验证 id regex，拒绝覆盖 bundled pets，验证 spritesheet 维度（1536×1872 v1 或 1536×2288 v2），推断 version
- `remove(pet_id)`：拒绝 bundled，删除目录，若删除的是 selected 重置为 "yuexinmiao"
- 持久化到 `data/pet/settings.json`，atomic write

#### PetStateBroker —— 事件投影

[state.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/state.py)：

`handle_event(session_id, event)` 根据 `type(event).__name__` 切换：
- `ThinkingEvent` → thinking/running
- `ToolCallStartEvent` → tool/running + tool name
- `ToolCallEndEvent` → review/failed based on `event.ok`
- `ErrorEvent` → failed
- `FinalEvent` → complete + `finished_at`

`snapshot()` 清理过期条目（`finished_at + ttl`），返回 `phase`, `message`, `animation`, `task`, `sessionId`, `activeTaskCount`。优先 `waiting_approval` 任务，否则最近更新的。

#### PetProcessManager —— 子进程

[process.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/process.py)：

`start()`：
- frozen：`sys.executable --pet --gateway-url ... --data-dir ...`
- source：`sys.executable -m claw.pet --gateway-url ... --data-dir ...`
- Windows `CREATE_NO_WINDOW`
- stdin/stdout/stderr → DEVNULL

`stop(timeout=3.0)` → terminate → wait → 超时则 kill + 1s wait。

#### 数据流

```
Agent Loop emits event
  → PetStateBroker.handle_event()
    → snapshot()
      → Gateway /pet/state endpoint
        → DesktopPet._poll_gateway() thread (1s)
          → _drain_updates() on Tk main thread (100ms)
            → _apply_remote_state()
              → animation switch + bubble refresh
```

### 4.12 Web UI 前端

**位置**：[webui/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/)

#### 应用结构

[App.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/App.tsx) 的 `Shell` 组件是中央协调器，拥有所有 session/message/UI 状态。没有全局 store（Redux/Zustand）——所有状态是提升到 Shell 的本地 React 状态。

`App` 包装 `Shell` 在 `ThemeProvider` + `ErrorBoundary` 中。

**Shell 状态**：view（chat/settings）, activeSessionId, settingsSection, messages, messagesLoading, sending, sidebarCollapsed, mobileSidebarOpen, isMobile, pendingApproval, autoMode, unlimitedMode, rollbackEnabled, rollingBack, workspaceRefreshToken。

**后台轮询**：
- 5s `fetchMessages`（空闲时，检测 cron 任务写的新消息；只在消息数增长时更新，避免覆盖进行中编辑）
- 10s `refreshSessions`（侧边栏反映其他会话的 cron 驱动更新）

**handleSend** 是最大最重要的处理器：
1. 无 activeSessionId 时懒创建
2. 上传所有附件
3. 构建 image markdown + 乐观用户消息
4. **斜杠命令分支**：`sendCommand` → 同步 auto/unlimited 标志 → 处理 actions（`open_pet_settings`, `reload_messages` 等）
5. **聊天发送分支**：阻塞 `POST /chat` 时两个轮询：`approvalTimer`（2s，poll `/approvals`）+ `msgTimer`（2s，poll `/messages` 显示 live tool-call 进度）

#### API 客户端

[api.ts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/lib/api.ts)：

- `API_BASE = ""`（相对路径，依赖 Vite proxy 或同源）
- `_pendingRequests: Map<string, Promise<any>>`（并发相同 GET 去重）
- 60s `AbortController` 超时
- `_parseJsonResponse` 检测 HTML fallback，抛友好中文错误
- `streamChat` SSE 客户端（已定义但未使用——当前用轮询）

#### ThreadViewport —— 消息渲染

[ThreadViewport.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/thread/ThreadViewport.tsx) 是最大组件：

**用户头像系统**：6 个内置（initial "U", person, cat, dog, fox, panda）+ custom（256×256 canvas center-crop WebP 0.88 quality，8MB 上限）。localStorage + 服务器同步。

**CodeBlock**：lazy `import("./CodeHighlighter")` + `CodeBlockErrorBoundary`（隔离 syntax-highlighter 崩溃）+ 复制按钮。

**MessageBubble**：四个分支
1. `role === "tool"` → 解析 JSON `{tool, ok, result, error}` → `LiveToolCall` → `ToolCallCard`
2. `role === "assistant"` with `tool_calls` → `InlineToolCalls`（紧凑展开摘要）
3. `role === "system"` → 居中 pill
4. user/assistant text → `ReactMarkdown` with `remarkGfm + remarkBreaks + remarkMath + rehypeKatex` + 自定义 `img`/`a`/`code`/`pre` 组件

**数学公式规范化** `normalizeMathMarkdown`：先拆分 code spans/blocks（避免代码内 `\[\]` 被误解析为数学），然后翻译 `\(...\)` → `$...$`，`\[...\]` → `$$...$$`。

**回退按钮**：`rollbackEnabled && message.rollbackAvailable && message.rollbackCheckpointId && !message.command` 时在用户气泡下显示 "回退" 按钮。

#### ThreadComposer —— 消息输入

[ThreadComposer.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/thread/ThreadComposer.tsx)：

- 附件处理（图片 ≤20MB，上限 4 张，object URLs）
- workspace picker（原生 `pickWorkspace()`）
- ↑/↓ 历史导航（每 session 独立历史）
- IME 安全 Enter（检查 `isComposing`/keyCode 229）
- 自动调整 textarea 高度（上限 200px）

#### ToolCallCard —— 工具调用展示

[ToolCallCard.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/thread/ToolCallCard.tsx)：

- 状态图标（`Loader2` spinning / `Check` / `X` red）
- duration（`Clock`，ms 或 seconds）
- 可折叠 args（截断 200 字符）+ result/error（截断 3000 字符，绿色/红色 tinted `<pre>`）

#### SettingsView —— 设置面板

[SettingsView.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/settings/SettingsView.tsx)：

8 个 section（prompt, soul, memory, channel, llm, cron, skills, pet）：
- **ChannelSection**：QQ 配置 + QR polling（2.5s 间隔）
- **LLMSection**：URL/model/ratios 验证
- **CronSection**：创建表单（target session, message, schedule kind）
- **SkillsSection**：上传 `.zip/.tar/.tar.gz/.tgz` + 列表/删除

#### PetSprite —— 空状态精灵

[PetSprite.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/PetSprite.tsx)：

5 个动画（idle, running-right, running-left, waving, jumping）。状态机：walk → idle/action 随机切换（40%/60%，每 5-10s）。直接操作 DOM（`el.style.backgroundPosition` + `el.style.transform`）避免 60fps React 重渲染。

#### Vite 配置

[vite.config.ts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/vite.config.ts)：

- 输出到 `../web`（兄弟目录），`emptyOutDir: true`
- 手动 chunk 分割：`vendor-react`, `vendor-markdown`, `vendor-syntax`, `vendor-icons`
- **Proxy**：正则覆盖所有后端路由前缀（`sessions|chat|stop|command|workspace|admin|memories|cron|approvals|skills|downloads|qq|reflect|pet`）+ `/api`。注释说明正则是必需的，否则 Vite 为未知路由返回 `index.html` 导致 JSON 解析崩溃。

### 4.13 Windows 桌面打包

**位置**：[packaging/windows/](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/packaging/windows/) + [claw/desktop.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/desktop.py)

#### desktop.py —— 桌面启动器

```python
def main() -> int:
    # --pet flag → 委托给 claw.pet.__main__.main（同一 frozen exe 可 spawn 宠物子进程）
    # --server-only → 前台运行 server
    # 否则：spawn server daemon thread → wait_until_ready → webview window
```

`_run_window(url)`：pywebview 创建 1280×820 窗口（min 960×640），`text_select=True`。缺失 webview 时打开系统浏览器并 sleep forever。

`_choose_port()`：读 `GATEWAY_PORT` env（默认 8000），不可用则 `sock.bind(("127.0.0.1", 0))` 绑定临时端口。

#### PyInstaller spec

[SJTUClaw.spec](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/packaging/windows/SJTUClaw.spec) 冻结 Python 程序到 `dist/SJTUClaw/SJTUClaw.exe`。

#### Inno Setup

[SJTUClaw.iss](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/packaging/windows/SJTUClaw.iss) 生成标准安装向导：
- 自选安装路径
- 开始菜单 + 桌面快捷方式
- 覆盖升级
- 系统卸载入口

可写数据默认保存到 `%APPDATA%\SJTUClaw\data`。

#### build.ps1 —— 一键构建

[build.ps1](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/packaging/windows/build.ps1)：
1. 安装依赖
2. 构建 WebUI
3. 检查 Tkinter
4. 运行 PyInstaller
5. 从 PATH 和常见安装目录自动查找 Inno Setup
6. 找不到 Inno Setup 仍保留可运行的 PyInstaller 目录版
7. `-SkipInstaller` 主动跳过安装向导

#### paths.py —— 三种部署模式路径切换

[paths.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/paths.py) 通过 `is_frozen()` 判断：
- **Source**：`resource_root()` = 项目根，`data_dir()` = `resource_root()/"data"`
- **PyInstaller**：`resource_root()` = `sys._MEIPASS`，`data_dir()` = `user_root()/"data"`（`%APPDATA%/SJTUClaw/data`）
- `prompts_dir()` / `skills_dir()`：frozen 时一次性复制 bundled → data_dir（仅在目标不存在时）

`user_root()` honors `SJTUCLAW_USER_DIR` env 覆盖。Windows 用 `%APPDATA%`/`%LOCALAPPDATA%`，POSIX 用 `$XDG_DATA_HOME`（默认 `~/.local/share`）。

#### runtime_settings.py —— 加密 WebUI 设置

[runtime_settings.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/runtime_settings.py)：

Fernet 加密存储 WebUI 编辑的 secrets，避免明文写回 `.env`。

```python
SECRET_KEYS = {"LLM_API_KEY", "COMPACT_LLM_API_KEY", "QQ_CLIENT_SECRET"}

def setting_value(name, default="") -> str:
    # 运行时设置优先于环境变量
    # 1. 检查 runtime_settings.json
    # 2. fallback to os.getenv
```

`load_runtime_settings(decrypt_secrets=False)` → 返回 `"********"` 占位符（当值解密非空时）。
`update_runtime_settings(updates)` → merge + 原子写入。

---

## 5. 关键类与函数参考

### Agent 核心

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `run_agent_turn(session_id, user_message, ...)` | [loop.py#L1679](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py) | 公共入口，包装 rollback |
| `_run_agent_turn_unlocked(...)` | [loop.py#L329](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py) | Think-Act-Observe 核心 |
| `_finish_reply(text, *, empty_reason, status)` | [loop.py#L602](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py) | 单一出口，保证恰好一条 assistant 消息 |
| `_handle_skill_select(...)` | [loop.py#L1728](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py) | use_skill 工具的审批门处理 |
| `IterationBudget` | [budget.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/budget.py) | 线程安全迭代计数器（CLAW_MAX_AGENT_ITERATIONS=15） |
| `TurnMetrics` | [metrics.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/metrics.py) | 单回合性能计数器 |
| `TurnMetricsAggregator` | [metrics.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/metrics.py) | 会话级 metrics 滚动窗口（200 turns） |
| `LoopHealthMonitor` | [health.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/health.py) | 跨回合健康监控（10 turns 窗口） |
| `TurnEvent` 及子类 | [events.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/events.py) | ThinkingEvent/ToolCallStart/End/Final/Error |
| `TurnContext` | [turn_context.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/turn_context.py) | per-turn 状态 bundle（refactor 目标） |

### Context

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `ContextBuilder.build_messages(...)` | [builder.py#L238](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/builder.py) | 唯一的 LLM messages 组装器 |
| `ContextBudget.measure(...)` | [budget.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/budget.py) | 不可变 token 预算快照 |
| `ContextGovernor.prepare_for_model(...)` | [governance.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/governance.py) | 8 步治理管道 |
| `compact_session(...)` | [compaction.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction.py) | 纯函数，不修改 session |
| `apply_compaction_result(session, result)` | [compaction.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction.py) | 唯一修改器，推进 last_consolidated |
| `CompactionWorker` | [compaction_worker.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/compaction_worker.py) | 后台线程 + 空闲压缩 |
| `count_tokens(text)` | [token_counter.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/context/token_counter.py) | tiktoken + CJK 启发式回退 |

### LLM

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `LLMClient` | [client.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/client.py) | OpenAI 兼容客户端 + 重试 |
| `LLMClient.chat_with_tools(...)` | [client.py#L144](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/client.py) | 工具调用入口 |
| `parse_agent_response(...)` | [protocol.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/protocol.py) | 解析 native + JSON 协议 |
| `AgentResponse` | [protocol.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/protocol.py) | `final`/`tool_calls`/`finish_reason` |
| `_scrub_secrets(text)` | [client.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/llm/client.py) | 日志 secret 清理 |

### Session

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `Session` | [models.py#L187](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/models.py) | 会话模型 |
| `Message` | [models.py#L47](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/models.py) | 消息模型 |
| `SessionStore` | [store.py#L158](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) | JSONL 持久化 + 缓存 |
| `SessionStore.save(session, *, fsync=False)` | [store.py#L473](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) | 原子写入 |
| `restore_runtime_checkpoint(session)` | [store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) | 崩溃恢复 |
| `fork_session_before_user_index(...)` | [store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/store.py) | 会话分叉 |
| `auto_title_if_first_turn(...)` | [title.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/session/title.py) | 自动标题 |

### Memory

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `MemoryStore` | [store.py#L323](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/store.py) | Markdown + YAML 文件系统存储 |
| `MemoryStore.recall(query, category, limit)` | [store.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/store.py) | 关键词 + CJK 字符召回 |
| `MemoryEntry` | [store.py#L188](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/store.py) | 单条记忆数据模型 |
| `ReflectionManager` | [reflection.py#L197](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/reflection.py) | 每日后台反思 |
| `ReflectionManager.run_now()` | [reflection.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/memory/reflection.py) | 手动触发 |

### Tools

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `ToolRegistry` | [base.py#L495](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) | 注册 + 执行 |
| `ToolRegistry.execute_by_name(name, args)` | [base.py#L585](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) | 永不抛出 |
| `Tool` | [base.py#L183](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) | 工具数据类 |
| `ToolResult` | [base.py#L161](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) | ok/content/error 不变量 |
| `ToolGuardrails` | [base.py#L443](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) | 每回合调用上限 |
| `register_all_tools(...)` | [__init__.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/__init__.py) | 注册编排 |
| `CronTool` | [cron_tool.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/cron_tool.py) | add/list/remove 定时任务 |

### Workspace

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `WorkspaceManager` | [manager.py#L60](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/manager.py) | 绑定 + 路径沙箱 |
| `WorkspaceManager.resolve(session_id, path_str, *, must_exist)` | [manager.py#L229](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/manager.py) | 路径解析 + 边界检查 |
| `WorkspaceRollbackManager` | [rollback.py#L66](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/rollback.py) | 检查点 + CAS + SQLite |
| `rollback(session_id, target=None)` | [rollback.py#L702](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/rollback.py) | 原子回退 + 补偿 |
| `recover_incomplete_operations()` | [rollback.py#L144](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/rollback.py) | 启动时崩溃恢复 |
| `garbage_collect()` | [rollback.py#L454](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/workspace/rollback.py) | CAS 对象 GC |

### Approval

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `ApprovalManager` | [manager.py#L89](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py) | 待审批队列 |
| `ApprovalManager.wait(approval_id, timeout)` | [manager.py#L179](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py) | 阻塞 + 超时自动拒绝 |
| `ApprovalRequest` | [manager.py#L55](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py) | 请求 dataclass |
| `ApprovalStatus` | [manager.py#L49](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/approval/manager.py) | PENDING/APPROVED/REJECTED |

### Scheduler

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `CronService` | [service.py#L333](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/service.py) | 持久化 + 计时器 |
| `CronService.add_job(...)` | [service.py#L1767](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/service.py) | 添加 job |
| `create_cron_dispatcher(...)` | [dispatcher.py#L43](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/dispatcher.py) | 路由到 agent loop |
| `HeartbeatCallback` | [callbacks.py#L25](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/callbacks.py) | Heartbeat 系统任务 |
| `CronJob` | [types.py#L81](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/types.py) | Job 实体 |
| `CronSchedule` | [types.py#L22](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/scheduler/types.py) | at/every/cron |

### Skills

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `SkillRegistry` | [registry.py#L229](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/registry.py) | 扫描 + 热重载 |
| `SkillRegistry.rescan(*, force=False)` | [registry.py#L470](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/registry.py) | 增量热重载 |
| `SkillInfo` | [registry.py#L81](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/registry.py) | Skill 元数据 |
| `SkillUsageStore` | [usage.py#L108](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/usage.py) | 使用统计 sidecar |
| `install_skill_package_bytes(...)` | [management.py#L79](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/skills/management.py) | 安全包安装 |

### Channels

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `BaseChannel` | [base.py#L55](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/base.py) | 抽象渠道基类 |
| `OutboundMessage` | [base.py#L22](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/base.py) | 出站消息 |
| `QQChannel` | [qq.py#L138](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/qq.py) | QQ Bot 适配器 |
| `qr_register(timeout_seconds)` | [qq_onboard.py#L176](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/qq_onboard.py) | QR 扫码登录 |
| `decrypt_secret(encrypted, key)` | [qq_crypto.py#L19](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/qq_crypto.py) | AES-256-GCM 解密 |
| `build_approval_keyboard(approval_id)` | [qq_interactions.py#L35](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/channels/qq_interactions.py) | 内联审批键盘 |

### Gateway

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| FastAPI `app` | [server.py#L506](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/server.py) | FastAPI 应用 |
| `RuntimeLLMClient` | [server.py#L105](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/server.py) | 可变 LLM 客户端 |
| `_lifespan(_app)` | [server.py#L444](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/server.py) | 生命周期 |
| `GatewaySecurityMiddleware` | [middleware.py#L61](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/middleware.py) | origin + token |
| `RateLimitMiddleware` | [middleware.py#L172](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/middleware.py) | 300 req/60s |
| `RequestSizeMiddleware` | [middleware.py#L212](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/middleware.py) | 10 MB / 50 MB |
| `save_upload_limited(...)` | [uploads.py#L14](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/gateway/uploads.py) | 流式上传 |

### Pet

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `DesktopPet` | [app.py#L290](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/app.py) | Tkinter 窗口 |
| `GatewayClient` | [app.py#L182](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/app.py) | HTTP 客户端 |
| `PetCatalog` | [catalog.py#L50](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/catalog.py) | 资源管理 |
| `PetProcessManager` | [process.py#L14](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/process.py) | 子进程管理 |
| `PetStateBroker` | [state.py#L31](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/state.py) | 事件投影 |
| `run_desktop_pet(gateway_url, data_dir)` | [app.py#L1156](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/pet/app.py) | 入口 |

### CLI

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `main()` | [main.py#L283](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/main.py) | sjtuclaw 命令 |
| `run_repl(...)` | [repl.py#L29](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/repl.py) | REPL 循环 |
| `handle_command(user_input, state, *, markdown)` | [commands.py#L227](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/commands.py) | 斜杠命令分发 |
| `RuntimeState` | [commands.py#L190](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/cli/commands.py) | CLI 共享状态 |

### Prompts

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `load_system_prompt()` | [__init__.py#L162](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/prompts/__init__.py) | 加载 system_prompt.md |
| `load_soul()` | [__init__.py#L176](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/prompts/__init__.py) | 加载 soul.md |
| `build_identity(...)` | [__init__.py#L103](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/prompts/__init__.py) | 渲染 identity.md |
| `render(template, variables)` | [templates.py#L21](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/prompts/templates.py) | Jinja2 子集渲染 |

### 工具函数

| 类/函数 | 位置 | 说明 |
|---------|------|------|
| `detect_system_timezone()` | [utils.py#L57](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/utils.py) | 时区检测（CLAW_TIMEZONE → tzlocal → TZ → /etc/localtime → Asia/Shanghai） |
| `atomic_write(path, content)` | [utils.py#L101](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/utils.py) | 原子写入 |
| `decode_subprocess_output(data)` | [utils.py#L116](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/utils.py) | 子进程输出解码（utf-8 → locale → mbcs/gb18030） |
| `force_utf8_stdio()` | [utils.py#L147](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/utils.py) | 强制 UTF-8 stdio |
| `load_config()` | [config.py#L137](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/config.py) | 加载 LLM 配置 |
| `setting_value(name, default)` | [runtime_settings.py#L120](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/runtime_settings.py) | 运行时设置 + env |

---

## 6. 依赖关系

### Python 依赖（requirements.txt + pyproject.toml）

**核心运行时**：

| 依赖 | 版本 | 用途 |
|------|------|------|
| `openai` | ≥1.0 / ==2.45.0 | OpenAI 兼容 API 客户端 |
| `python-dotenv` | ≥1.0 / ==1.1.0 | `.env` 文件加载 |
| `fastapi` | ≥0.100 / ==0.121.1 | REST API 框架 |
| `uvicorn` | ≥0.20 / ==0.38.0 | ASGI 服务器 |
| `python-multipart` | ≥0.0.5 / ==0.0.32 | 文件上传解析 |
| `tiktoken` | ≥0.5 / ==0.13.0 | Token 计数（o200k_base） |
| `filelock` | ≥3.0 | 跨进程文件锁（CronService, SkillUsageStore） |
| `Pillow` | ≥10.0 / ==10.4.0 | 图片处理（宠物精灵、附件） |
| `PyYAML` | ≥6.0 / ==6.0.2 | YAML frontmatter 解析 |
| `croniter` | ≥2.0 / ==6.2.3 | cron 表达式解析 |
| `aiohttp` | ≥3.8 / ==3.11.10 | QQ WebSocket 客户端 |
| `httpx` | ≥0.25 / ==0.28.1 | HTTP 客户端（LLM, QQ REST, web tools） |
| `qrcode` | ≥7.0 / ==8.2 | QR 码生成（QQ onboard） |
| `cryptography` | ≥41.0 / ==44.0.1 | AES-256-GCM（QQ secret）+ Fernet（runtime settings） |
| `tzlocal` | ≥5.0 / ==5.3.1 | 本地时区检测 |

**可选依赖**：

| 依赖 | 版本 | 用途 |
|------|------|------|
| `pywebview` | ≥5.0 / ==6.2.1 | Windows 桌面窗口（`[desktop]`） |
| `pyinstaller` | ≥6.0 / ==6.21.0 | 打包（`[build]`） |

**测试**：

| 依赖 | 版本 | 用途 |
|------|------|------|
| `pytest` | ==9.1.1 | 后端测试 |

### 模块间依赖关系

```
claw/main.py ─┬─→ claw/cli/repl.py ─→ claw/agent/loop.py
              ├─→ claw/context/builder.py ─→ claw/memory/store.py
              ├─→ claw/context/compaction_worker.py
              ├─→ claw/scheduler/service.py ─→ claw/scheduler/dispatcher.py
              ├─→ claw/memory/reflection.py
              ├─→ claw/pet/process.py
              └─→ claw/skills/registry.py

claw/gateway/server.py ─┬─→ claw/agent/loop.py
                       ├─→ claw/context/* 
                       ├─→ claw/session/store.py
                       ├─→ claw/memory/*
                       ├─→ claw/skills/*
                       ├─→ claw/tools/* (register_all_tools)
                       ├─→ claw/workspace/{manager,rollback}.py
                       ├─→ claw/approval/manager.py
                       ├─→ claw/scheduler/*
                       ├─→ claw/channels/qq.py
                       ├─→ claw/pet/*
                       └─→ claw/runtime_settings.py

claw/agent/loop.py ─┬─→ claw/llm/client.py ─→ claw/llm/protocol.py
                   ├─→ claw/context/builder.py
                   ├─→ claw/tools/base.py (ToolRegistry)
                   ├─→ claw/approval/manager.py
                   ├─→ claw/session/store.py
                   └─→ claw/agent/{metrics,health,events,budget}.py

claw/tools/* ─→ claw/workspace/manager.py
            ─→ claw/memory/store.py
            ─→ claw/skills/registry.py
            ─→ claw/scheduler/service.py
```

### 前端依赖（webui/package.json）

**运行时**：
- React 18.3 + react-dom
- Radix UI（dialog, dropdown-menu, separator, slot, tooltip）
- `class-variance-authority`, `clsx`, `tailwind-merge`（Tailwind 工具组合）
- `lucide-react`（图标）
- `react-markdown` + `remark-gfm`, `remark-breaks`, `remark-math`, `rehype-katex`, `katex`（Markdown + LaTeX）
- `react-syntax-highlighter`（代码高亮）

**开发时**：
- Tailwind 3.4 + typography + animate
- TypeScript 5.7
- Vite 5.4
- Vitest 4.1 + jsdom + Testing Library

---

## 7. 项目运行方式

### 环境要求

- **Python**：3.11+
- **Node.js**：18+（仅前端开发或重新构建 Web UI 时需要）
- **OpenAI 兼容模型服务**：OpenAI、Ollama、vLLM 或 LM Studio
- **Windows 打包**：Inno Setup 7（可选，生成安装包时需要）

### 源码运行

#### 安装与配置

```bash
# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# 2. 安装依赖
python -m pip install -r requirements.txt
python -m pip install -e .

# 3. 交互式配置
sjtuclaw setup
```

`setup` 向导会引导配置：
- LLM 凭据（`LLM_API_KEY`, `LLM_BASE_URL` 默认 `https://api.openai.com/v1`, `LLM_MODEL` 默认 `gpt-4o`）
- QQ Bot（可选，QR 扫码或手动 AppID/AppSecret/AllowFrom）

也可以复制 `.env.example` 为 `.env` 手动配置。

#### 启动

```bash
sjtuclaw chat       # CLI 交互对话
sjtuclaw gateway    # Gateway + Web UI + REST API
sjtuclaw-desktop    # Desktop：本地 Gateway + pywebview 独立窗口
```

Gateway 启动后访问 <http://127.0.0.1:8000>。

#### 前端开发

```bash
cd webui
npm install
npm run dev         # http://127.0.0.1:5173
```

开发时 Vite proxy 将所有后端路由前缀转发到 `http://127.0.0.1:8000`（可通过 `SJTUCLAW_API_URL` env 覆盖）。

### Windows 桌面版

运行发布目录中的 `SJTUClaw-Setup-0.1.0.exe`，按安装向导选择安装位置和快捷方式。

安装版可写数据默认保存在：
```
%APPDATA%\SJTUClaw\data
```

包括会话、记忆、模型设置、定时任务、用户 Skill 和宠物资源。重新安装或覆盖升级不会主动删除用户数据。

### AUTO 与 UNLIMITED 模式

两个相互独立、按 Session 生效的执行模式；新建 Session 时二者默认均为关闭状态，Gateway 重启后也恢复为关闭状态。

| 模式 | 作用 | 审批行为 | 文件系统边界 |
|------|------|----------|--------------|
| 默认 | 完整安全保护 | 写入和 Shell 逐次审批 | 仅 workspace |
| AUTO | 减少 workspace 内操作确认 | 自动批准写入和 Shell；Skill 加载确认保留 | 严格 workspace，越界拒绝 |
| UNLIMITED | 解除路径限制 | 写入/覆盖/删除/Shell 始终逐次审批，AUTO 无法跳过 | 可访问 workspace 外 |

```text
/auto on|off|toggle|status
/unlimited on|off|toggle|status
```

> AUTO ≠ 取消安全边界：只省略 workspace 沙箱内写入和 Shell 的逐次审批。UNLIMITED 才会解除路径边界，但不取消危险操作审批。两模式同时开启时，UNLIMITED 的强制审批规则优先。

### Workspace 回退

为 Session 设置 workspace 后，系统自动在每次用户消息执行前创建检查点；未设置 workspace 时不启用回退。

```text
/rollback                 # 回退一轮
/rollback 3               # 回退到倒数第 3 个用户回合之前
/rollback <checkpointId>  # 回退到指定检查点
/rollback list            # 列出可用检查点
/rollback status          # 查看状态
/rollback undo            # 撤销最近一次回退
```

### 常用操作

```text
/session new|list|switch|rename|delete
/workspace set|show|unset
/rollback [n|checkpointId]|list|status|undo
/compact
/memory add|list|search|update|delete|stats
/reflect status|enable|disable|time|now
/skill list|show|usage|<name>
/auto on|off|toggle|status
/unlimited on|off|toggle|status
/cron list|status|disable|enable|delete
/approvals|approve|reject
/pet status|list|open|close|select|autostart
/stop
/help
```

也可以直接用自然语言创建定时任务、保存记忆或请求使用 Skill。

### 关键环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `LLM_API_KEY` | 必填 | 模型服务密钥 |
| `LLM_BASE_URL` | 必填 | 服务地址 |
| `LLM_MODEL` | 必填 | 模型名称 |
| `LLM_CONTEXT_WINDOW` | 32000 | 上下文窗口大小 |
| `LLM_CONTEXT_USAGE_RATIO` | 0.80 | 上下文使用比例 |
| `LLM_MAX_OUTPUT_TOKENS` | 4096 | 最大输出 tokens |
| `LLM_CONSOLIDATION_RATIO` | 0.5 | 整合比例 |
| `CLAW_TIMEZONE` | 自动 | 时区覆盖 |
| `CLAW_MAX_AGENT_ITERATIONS` | 15 | 最大迭代次数 |
| `CLAW_MAX_TOOL_CALLS_PER_TURN` | 20 | 每回合工具上限 |
| `CLAW_MAX_IDENTICAL_TOOL_CALLS` | 3 | 卡循环检测阈值 |
| `GATEWAY_HOST` | 127.0.0.1 | Gateway 主机 |
| `GATEWAY_PORT` | 8000 | Gateway 端口 |
| `GATEWAY_API_TOKEN` | 无 | 非 loopback 必填 |
| `GATEWAY_OPEN_BROWSER` | false | 启动时打开浏览器 |
| `QQ_ENABLED` | false | 启用 QQ Bot |
| `QQ_APP_ID` | 无 | QQ AppID |
| `QQ_CLIENT_SECRET` | 无 | QQ Secret |
| `QQ_ALLOW_FROM` | 无 | 允许的用户（逗号分隔） |
| `QQ_MSG_FORMAT` | markdown | 消息格式 |
| `HEARTBEAT_ENABLED` | true | 启用 Heartbeat |
| `HEARTBEAT_INTERVAL_S` | 1800 | Heartbeat 间隔 |
| `COMPACT_*` | 见配置 | 压缩相关 |
| `SJTUCLAW_USER_DIR` | 自动 | 用户数据目录覆盖 |
| `SJTUCLAW_DATA_DIR` | 自动 | 数据目录覆盖 |
| `WSS_PROXY`/`HTTPS_PROXY` | 无 | QQ WebSocket 代理 |

完整配置说明见 [docs/configuration.md](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/docs/configuration.md)。

---

## 8. 测试与构建

### 后端测试

```bash
pytest tests/
```

测试覆盖：
- Agent Loop（[test_cancel_turn.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_cancel_turn.py), [test_agent_tool_reply.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_agent_tool_reply.py)）
- 上下文压缩（[test_compaction.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_compaction.py)）
- Reflection（[test_reflection.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_reflection.py)）
- Skill 管理（[test_skill_management.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_skill_management.py)）
- Workspace 回退（[test_workspace_rollback.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_workspace_rollback.py), [test_cli_rollback_integration.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_cli_rollback_integration.py)）
- Cron 集成（[test_cron_integration.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_cron_integration.py)）
- Gateway（[test_gateway_fixes.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_gateway_fixes.py), [test_gateway_rollback.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_gateway_rollback.py)）
- 安全加固（[test_security_hardening.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_security_hardening.py), [test_skill_cron_hardening.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_skill_cron_hardening.py)）
- Web 工具（[test_web_tools.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_web_tools.py)）
- QQ 媒体和 Web 图片（[test_qq_media_and_web_images.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_qq_media_and_web_images.py)）
- 宠物（[test_pet.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_pet.py), [test_pet_command.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_pet_command.py)）
- 编码（[test_encoding.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_encoding.py)）
- 运行时设置（[test_runtime_settings.py](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/tests/test_runtime_settings.py)）

### 前端测试

```bash
cd webui
npm test            # Vitest
```

测试辅助：
- [webui/src/hooks/useDragScroll.test.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/hooks/useDragScroll.test.tsx)
- [webui/src/components/sidebar/Sidebar.test.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/sidebar/Sidebar.test.tsx)
- [webui/src/components/thread/ThreadComposer.test.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/thread/ThreadComposer.test.tsx)
- [webui/src/components/thread/ThreadViewport.test.tsx](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/components/thread/ThreadViewport.test.tsx)
- [webui/src/lib/commandState.test.ts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/lib/commandState.test.ts)
- [webui/src/lib/commands.test.ts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/lib/commands.test.ts)
- [webui/src/lib/utils.test.ts](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/webui/src/lib/utils.test.ts)

### 构建产物

**前端**：
```bash
cd webui
npm run build
```
输出到 `web/`（兄弟目录），`emptyOutDir: true`，无 sourcemaps，ES2020 target。

**Windows 安装包**：

```powershell
.\packaging\windows\build.ps1
```

构建脚本流程：
1. 安装依赖
2. 构建 WebUI
3. 检查 Tkinter
4. 运行 PyInstaller
5. 自动查找 Inno Setup（PATH + 常见安装目录）
6. 找不到 Inno Setup 仍保留可运行的 PyInstaller 目录版
7. `-SkipInstaller` 主动跳过安装向导

构建产物：
```text
dist\SJTUClaw\SJTUClaw.exe                # PyInstaller 目录版
dist\installer\SJTUClaw-Setup-0.1.0.exe   # Inno Setup 安装包
```

> 修改 Python 或 WebUI 源码后必须重新运行构建脚本。`dist/` 中已有的 EXE 和安装包不会自动包含最新源码。

详细说明见 [docs/windows-packaging.md](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/docs/windows-packaging.md)。

### 源码 vs 安装版差异

| 维度 | 源码运行 | 安装版 |
|------|----------|--------|
| 资源根 | 项目根 | `sys._MEIPASS` |
| 数据目录 | `resource_root()/"data"` | `%APPDATA%/SJTUClaw/data` |
| .env 位置 | 项目根 `.env` | `user_root()/.env` |
| prompts/skills | 直接读 bundled | 一次性复制 bundled → data_dir |
| 启动入口 | `sjtuclaw` / `sjtuclaw-desktop` | `SJTUClaw.exe` |
| 共用 | 同一套 Agent/Tool/Memory/Skill/Scheduler/Workspace/Gateway 代码 | 同上 |

两种方式共用同一套 Agent、Tool、Memory、Skill、Scheduler、Workspace 回退和 Gateway 代码，主要区别在启动入口、资源路径和运行数据位置。

---

## 附录：关键设计模式与不变量

### 1. 单一 Agent Loop 入口不变量

所有用户交互（CLI/Gateway/QQ/Cron/Heartbeat）必须通过 [run_agent_turn()](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py)，绝不直接调用 `LLMClient`。这保证：
- 一致的工具调用语义
- 一致的安全审批流程
- 一致的 metrics 与 health 监控
- 一致的 Skill 注入协议

### 2. 持久化历史不可变不变量

上下文压缩只推进 `session.last_consolidated` 投影边界，**绝不删除**原始消息。`get_unconsolidated_messages()` 返回 `messages[last_consolidated:]`。原始 transcript 永远可审计、可回退。

### 3. 回退原子性不变量

回退操作通过 SQLite WAL + 操作日志（PREPARED → FILES_APPLIED → COMMITTED/COMPENSATED/FAILED）保证原子性。异常时通过 safety checkpoint 补偿。`recover_incomplete_operations()` 在启动时幂等补偿中断的操作。文件内容用 SHA-256 CAS（hash + 重验证 + 原子 replace）保证完整性。

### 4. 工具执行永不抛出不变量

[ToolRegistry.execute_by_name()](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/tools/base.py) 在 try/except 中执行 handler，**永不抛出**。所有错误通过 `ToolResult(ok=False, error=...)` 返回，保证 Agent Loop 不会因工具异常而崩溃。

### 5. 单一出口不变量

[_finish_reply()](file:///c:/Users/GZQ/Desktop/SJTUClaw/SJTUClaw/claw/agent/loop.py) 是 Agent Loop 的唯一出口，用 `reply_finished` nonlocal 标志保证恰好一条 assistant 消息被持久化、恰好一个 `FinalEvent` 被发出、metrics/health 总是被记录。

### 6. 快照式并发不变量

CompactionWorker 在短暂加锁后立即释放，慢速 LLM 调用在锁外执行。应用结果时检查 `session.revision == snapshot_revision`——若不一致则丢弃陈旧摘要，永不丢失新消息或覆盖回退后的状态。

### 7. Skill 注入审批门不变量

当模型调用 `use_skill` 时，Agent Loop 在用户确认**之前**暂停——模型只在用户批准后才看到完整 Skill 指令。这防止 Skill 内容未授权进入 LLM 上下文。

### 8. CJK 友好性

整个项目针对中文用户优化：
- Token 计数使用 CJK 感知启发式
- 所有用户可见消息为中文
- Prompt 模板使用中文
- 终端 GBK 兼容（`force_utf8_stdio`, `decode_subprocess_output`, QR 渲染 GBK workaround）
- 文件编码防乱码（项目记忆中强调 UTF-8）

---

> 本文档基于代码库当前状态生成，最后更新时间：2026-07-20。
