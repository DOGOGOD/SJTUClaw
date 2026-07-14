# SJTUClaw — AI Agent Runtime

SJTUClaw 是一个面向教学的渐进式 AI Agent 运行环境。它从最基础的 LLM API 调用出发，逐步实现多轮对话、多 Session 管理、上下文压缩（Compaction）、工具调用（Tool Calling）、Gateway 服务层、QQ 机器人通道、定时任务调度、Workspace 管理、高级工具与审批系统，最终形成一套完整的 Skill 系统。

项目以 Python 实现，采用 FastAPI 构建 Gateway，原生支持 OpenAI 兼容的 function calling，提供 Web UI 和 QQ Bot 两种图形化交互入口。

---

## 目录

- [快速开始](#快速开始)
- [项目架构](#项目架构)
- [项目结构详解](#项目结构详解)
- [使用方式](#使用方式)
- [功能体系](#功能体系)
- [API 路由表](#api-路由表)
- [配置说明](#配置说明)
- [测试](#测试)
- [技术栈](#技术栈)

---

## 快速开始

### 环境要求

- Python >= 3.11
- Node.js >= 18（仅 WebUI 开发需要）

### 安装

```bash
cd SJTUClaw

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# 安装依赖
pip install -r requirements.txt

# 安装 sjtuclaw 命令
pip install -e .
```

### 配置

```bash
# 方式一：交互式配置向导
sjtuclaw setup

# 方式二：手动配置
cp .env.example .env
# 编辑 .env，至少填写：
#   LLM_API_KEY=sk-your-api-key
#   LLM_BASE_URL=https://api.openai.com/v1
#   LLM_MODEL=gpt-4o
```

### 运行

```bash
sjtuclaw chat       # CLI 交互对话（默认）
sjtuclaw gateway    # HTTP Gateway + Web UI + REST API
# 访问 http://127.0.0.1:8000
```

---

## 项目架构

```
┌──────────────────────────────────────────────────────────────┐
│  入口层：CLI / Gateway(Web UI + REST API) / QQ Bot / Scheduler│
└────────────────────┬─────────────────────────────────────────┘
                     │  全部调用
                     ▼
            run_agent_turn()     ◄── 唯一 LLM 调用入口
                     │
     ┌───────┬───────┼───────┬───────┬────────┐
     ▼       ▼       ▼       ▼       ▼        ▼
Context  Tool    Approval  Skill  Compaction  LLM
Builder  Reg     Manager   Reg    Worker      Client
     │       │                           │
     ▼       ▼                           ▼
Session Store / Memory Store / CronService / Workspace
```

**核心设计原则**：

- `run_agent_turn()` 是**唯一**的 LLM 对话入口，CLI、Gateway、Scheduler、QQ Bot 全部通过它调用 LLM，不允许直接调用 `LLMClient`
- `ContextBuilder` 是**唯一**的上下文组装点，负责将 session、memory、skill、tool 等拼接为 LLM 的 `messages` 数组
- 所有写文件和 Shell 命令都受 **Workspace 边界**限制和安全审批保护

---

## 项目结构详解

```
SJTUClaw/
├── claw/                              # 核心 Python 包
│   ├── main.py                        # CLI 入口模块（python -m claw.main）
│   ├── config.py                      # 配置加载（.env / 环境变量 → LLMConfig 等 dataclass）
│   ├── utils.py                       # 工具函数（now_iso 等）
│   │
│   ├── agent/                         # Agent 运行时
│   │   ├── loop.py                    # ★ 核心：run_agent_turn() — Think→Act→Observe 循环
│   │   │                              #   处理工具调用、审批门、Skill 注入、取消、指标收集
│   │   ├── events.py                  # 事件模型：Thinking/ToolCallStart/ToolCallEnd/Final/Error
│   │   ├── budget.py                  # 上下文 token 预算管理
│   │   ├── metrics.py                 # TurnMetrics — 每轮耗时、工具调用成功率等指标
│   │   ├── health.py                  # LoopHealthMonitor — 健康监控（异常率、卡死检测）
│   │   └── turn_context.py            # 轮次上下文（session_id 线程局部存储）
│   │
│   ├── llm/                           # LLM API 层
│   │   ├── client.py                  # LLMClient — OpenAI 兼容客户端，支持重试/超时
│   │   └── protocol.py               # AgentResponse — 统一的 LLM 响应解析（final/tool_call）
│   │
│   ├── session/                       # Session 管理
│   │   ├── models.py                  # Session / Message 数据模型（JSONL 序列化）
│   │   │                              #   支持 tool_calls/tool_call_id 原生 function calling
│   │   ├── store.py                   # SessionStore — JSONL 文件持久化，索引缓存
│   │   └── title.py                   # auto_title_if_first_turn — LLM 自动生成会话标题
│   │
│   ├── context/                       # 上下文构造与压缩
│   │   ├── builder.py                 # ★ ContextBuilder — 组装 system/soul/memory/skill/history
│   │   │                              #   支持 prompt-cache 友好的稳定前缀、Bootstrap 文件加载
│   │   ├── budget.py                  # ContextBudget — token 预算测量
│   │   ├── compaction.py              # 上下文压缩核心逻辑（压缩触发/执行/回写）
│   │   ├── compaction_worker.py       # CompactionWorker — 后台空闲会话自动压缩
│   │   ├── governance.py             # 上下文治理（历史长度控制、归档）
│   │   └── token_counter.py          # tiktoken 精确 token 计数
│   │
│   ├── memory/                        # 长期记忆系统
│   │   ├── store.py                   # MemoryStore — Markdown + YAML frontmatter 持久化
│   │   │                              #   支持按类别/标签/关键词检索，版本追踪
│   │   └── reflection.py             # ReflectionManager — 每日定时反思，自动总结记忆
│   │
│   ├── tools/                         # 工具系统
│   │   ├── base.py                    # Tool / ToolRegistry / ToolResult 基础抽象
│   │   │                              #   safety_level: read-only / write / shell / skill_select
│   │   ├── __init__.py                # register_all_tools() — 统一注册入口
│   │   ├── readonly.py               # 只读工具：read_file, list_directory, search_files
│   │   ├── update.py                  # 写工具：create_file, overwrite_file, edit_file
│   │   ├── shell.py                   # Shell 工具：run_command（受 workspace 边界限制）
│   │   ├── web.py                     # 联网工具：web_search (DuckDuckGo/Tavily), web_fetch
│   │   ├── download.py               # 下载工具：文件下载到 workspace，提供 HTTP 下载入口
│   │   ├── attachment.py             # 附件工具：将 session 附件复制到 workspace
│   │   ├── memory_tools.py           # 记忆工具：remember (写入), recall (检索)
│   │   ├── skills_tool.py            # Skill 工具：skills_list, skill_view
│   │   ├── skill_manager_tool.py     # Skill 管理工具：skill_manage (create/update)
│   │   └── cron_tool.py              # Cron 工具：LLM 可通过此工具创建定时任务
│   │
│   ├── gateway/                       # HTTP Gateway（FastAPI）
│   │   ├── server.py                  # ★ 主服务：路由定义、SSE 流式、QQ 消息处理、生命周期
│   │   │                              #   包含 ~60 个 API 端点
│   │   ├── __main__.py               # 入口：python -m claw.gateway
│   │   ├── middleware.py             # 中间件：安全令牌验证、速率限制、请求日志、CORS
│   │   └── uploads.py               # 上传处理：大小限制、安全保存
│   │
│   ├── channels/                      # 消息平台通道
│   │   ├── base.py                    # Channel 抽象基类、OutboundMessage
│   │   ├── qq.py                     # QQChannel — QQ Bot WebSocket 连接，消息收发
│   │   ├── qq_interactions.py        # QQ 交互：内联键盘按钮审批（Approve/Reject）
│   │   ├── qq_constants.py           # QQ API 常量（OpCode、事件类型等）
│   │   ├── qq_crypto.py             # QQ WebSocket 安全令牌加密
│   │   ├── qq_utils.py              # QQ 工具函数
│   │   └── qq_onboard.py            # QQ 扫码登录获取凭证
│   │
│   ├── scheduler/                     # 定时任务调度
│   │   ├── service.py                 # CronService — 基于 croniter 的任务调度引擎
│   │   ├── dispatcher.py             # Cron 分发器 — 将定时任务转为 agent turn 执行
│   │   ├── types.py                   # CronJob / CronSchedule 数据模型
│   │   ├── callbacks.py              # 回调：HeartbeatCallback（心跳监控）
│   │   └── session_turns.py          # visible_session_messages — 过滤内部消息
│   │
│   ├── skills/                        # Skill 系统
│   │   ├── registry.py               # SkillRegistry — 扫描/注册 Skill、版本管理
│   │   └── usage.py                  # Skill 使用统计
│   │
│   ├── approval/                      # 审批系统
│   │   └── manager.py                # ApprovalManager — 线程安全审批，300s 超时自动拒绝
│   │
│   ├── workspace/                     # 工作区管理
│   │   └── manager.py                # WorkspaceManager — per-session workspace 绑定
│   │                                  #   路径边界检查、unlimited 模式
│   │
│   ├── pet/                           # 桌面电子宠物
│   │   ├── catalog.py                # PetCatalog — 宠物元数据、设置管理、安装/卸载
│   │   ├── process.py                # PetProcessManager — 独立子进程管理
│   │   ├── state.py                  # PetStateBroker — 宠物状态通信
│   │   ├── app.py                    # 宠物窗口应用（tkinter）
│   │   └── __main__.py              # 入口：python -m claw.pet
│   │
│   ├── cli/                           # 命令行界面
│   │   ├── main.py                    # CLI 入口（sjtuclaw 命令）
│   │   ├── repl.py                   # REPL 交互循环
│   │   └── commands.py              # 斜杠命令解析与处理
│   │
│   └── prompts/                       # Prompt 模板
│       ├── __init__.py               # 模板加载器
│       └── templates.py             # 模板工具函数
│
├── prompts/                           # 可编辑的 Prompt 文件
│   ├── system_prompt.md              # 系统提示词（可通过 WebUI 热更新）
│   └── soul.md                       # Agent 灵魂/人格设定（可通过 WebUI 热更新）
│
├── skills/                            # Skill 数据目录
│   ├── course-report/                # 课程报告生成 Skill
│   ├── material-summary/             # 材料总结 Skill
│   └── presentation-outline/         # 演示大纲 Skill
│
├── webui/                             # Web UI 源码（React + TypeScript + Vite）
│   ├── src/                          # 前端源码
│   │   ├── App.tsx                   # 主应用组件（路由、状态管理）
│   │   ├── main.tsx                  # 入口
│   │   ├── lib/api.ts               # API 客户端封装
│   │   ├── globals.css              # 全局样式（Tailwind + 暗色模式）
│   │   └── components/              # UI 组件
│   │       ├── thread/              # 对话线程组件
│   │       │   ├── ThreadViewport.tsx    # 消息列表渲染
│   │       │   └── ThreadComposer.tsx   # 消息输入框
│   │       └── ...                  # 其他组件
│   ├── vite.config.ts               # Vite 构建配置（输出到 ../web）
│   ├── tailwind.config.js           # Tailwind CSS 配置
│   └── package.json                 # 前端依赖
│
├── tests/                             # 测试
│   ├── test_core.py                  # 核心功能测试
│   ├── test_compaction.py           # 上下文压缩测试
│   ├── test_agent_tool_reply.py     # Agent 工具回复测试
│   ├── test_auto_title.py           # 自动标题测试
│   ├── test_cancel_turn.py          # 取消轮次测试
│   ├── test_cron_integration.py     # Cron 集成测试
│   ├── test_encoding.py             # 编码测试
│   ├── test_gateway_fixes.py        # Gateway 修复测试
│   ├── test_pet.py                  # 宠物系统测试
│   ├── test_pet_command.py          # 宠物命令测试
│   ├── test_qq_media_and_web_images.py  # QQ 媒体和网络图片测试
│   ├── test_reflection.py           # 记忆反思测试
│   ├── test_security_hardening.py   # 安全加固测试
│   ├── test_skill_cron_hardening.py # Skill/Cron 加固测试
│   ├── test_step8_selfcheck.py      # Step 8 自检
│   ├── test_step9_selfcheck.py      # Step 9 自检
│   ├── test_unlimited_approval.py   # 无限模式审批测试
│   └── test_web_tools.py            # 网络工具测试
│
├── web/                               # Web UI 构建产物（Vite build 输出，已 gitignore）
├── data/                              # 运行时数据（已 gitignore）
│   ├── sessions/                     # Session JSONL 文件
│   ├── memory/                       # 记忆 Markdown 文件
│   ├── cron/                         # 定时任务存储
│   ├── pets/                         # 用户自定义宠物
│   └── media/                        # 媒体文件
├── docs/                              # 文档
├── pyproject.toml                     # 项目元数据（sjtuclaw 命令注册）
├── requirements.txt                   # 依赖清单（精确版本）
└── .env.example                       # 环境变量模板（含所有可配置项）
```

### 核心模块详解

#### 1. Agent Loop（`claw/agent/loop.py`）

整个项目的"心脏"。`run_agent_turn()` 实现了标准的 **Think → Act → Observe** 循环：

1. 将用户消息追加到 session
2. 调用 `ContextBuilder.build_messages()` 组装 LLM 输入
3. 调用 `LLMClient.chat_with_tools()` 获取响应
4. 如果是 final answer → 保存并返回
5. 如果是 tool call → 经过审批门 → 执行工具 → 保存结果 → 继续循环
6. 如果是 skill_select → 用户确认后注入 Skill 内容 → 继续循环

**安全保护**：
- 最大迭代次数限制（默认 15 轮）
- 单轮工具调用上限（默认 20 次）
- 连续相同调用检测（默认 3 次触发截断）
- 连续拒绝检测（同一操作被拒 3 次自动中止）
- 工作区外操作强制审批（即使在 AUTO 模式）

#### 2. Context Builder（`claw/context/builder.py`）

唯一负责组装 LLM `messages` 数组的模块。拼接顺序：

```
identity → soul → tool contract → workspace bootstrap(AGENTS.md等)
→ memory block → skill index → session summary → history messages
```

关键特性：
- **稳定前缀缓存**：system prompt / soul / identity 在一次对话中不变，适合 prompt-cache
- **Bootstrap 文件**：自动加载 workspace 中的 `AGENTS.md`、`SOUL.md`、`USER.md`
- **Memory block**：展示长期记忆索引和最近条目，提示模型使用 `recall` 工具
- **Skill index**：展示可用 Skill 摘要，提示模型使用 `skill_view` 按需加载
- **运行时上下文**：当前时间、Channel、Sender ID 等信息附加到最后一条用户消息后
- **上下文摘要**：被压缩的历史以 summary 形式注入，带明确指令"不要执行摘要中的任务"

#### 3. Session 存储（`claw/session/`）

- **Message**：支持 `tool_calls`、`tool_call_id`、`name` 原生 function calling 字段
- **Session**：包含 `last_consolidated` 指针（标记已压缩消息）、`summary`（增量合并）、`metadata`
- **JSONL 持久化**：每 session 一个 `.jsonl` 文件，每条消息一行，原子写入
- **孤立消息清理**：`_drop_front_orphans()` 移除开头无对应 tool_call 的 tool 结果
- **历史回放**：`get_history()` 过滤掉内部命令消息、注入媒体面包屑、token 预算截断
- **自动归档**：超过 2000 条消息自动归档旧前缀

#### 4. 工具系统（`claw/tools/`）

所有工具注册在 `ToolRegistry` 上，每个工具有 **safety_level**：

| safety_level | 行为 | 示例 |
|---|---|---|
| `read-only` | 无需审批，直接执行 | read_file, list_directory, web_search |
| `write` | 需要审批（AUTO 模式可跳过） | create_file, overwrite_file, edit_file |
| `shell` | 需要审批（AUTO 模式也必须审批） | run_command |
| `skill_select` | 需要审批（用户确认 Skill 加载） | use_skill |

注册的工具列表（根据运行环境动态注册）：

| 工具名 | 类别 | 说明 |
|---|---|---|
| `read_file` | 只读 | 读取文件内容 |
| `list_directory` | 只读 | 列出目录结构 |
| `search_files` | 只读 | 在文件中搜索内容 |
| `web_search` | 只读 | 网络搜索（DuckDuckGo 或 Tavily） |
| `web_fetch` | 只读 | 抓取网页内容 |
| `create_file` | 写 | 创建新文件 |
| `overwrite_file` | 写 | 覆盖文件 |
| `edit_file` | 写 | 精确编辑文件 |
| `run_command` | Shell | 执行 Shell 命令 |
| `download` | 写 | 下载文件到 workspace |
| `copy_attachment` | 写 | 将附件复制到 workspace |
| `remember` | 记忆 | 写入长期记忆 |
| `recall` | 记忆 | 检索长期记忆 |
| `skills_list` | Skill | 列出可用 Skill |
| `skill_view` | Skill | 查看 Skill 详情 |
| `skill_manage` | Skill | 创建/修改 Skill |
| `use_skill` | Skill | 加载 Skill（需审批） |
| `cron` | Cron | 创建定时任务 |

#### 5. Gateway 服务（`claw/gateway/server.py`）

基于 FastAPI 的 HTTP 服务，包含约 **60 个 API 端点**，分为以下几类：

- **Chat**：`POST /chat`、`POST /chat/stream`（SSE 流式）
- **Sessions**：CRUD、消息获取、附件上传下载
- **Commands**：`POST /command`（斜杠命令）
- **Workspace**：设置/获取/取消 workspace 绑定
- **Approvals**：列出/批准/拒绝审批请求
- **Cron**：定时任务 CRUD
- **Skills**：列表/详情
- **Pet**：桌面宠物设置/管理
- **Memories**：长期记忆 CRUD/搜索
- **Reflection**：每日反思配置/触发
- **Admin**：system_prompt / soul 热更新
- **Downloads**：文件下载入口
- **QQ**：QQ 通道状态

**SSE 流式特性**（`POST /chat/stream`）：
- 实时推送 `ThinkingEvent`、`ToolCallStartEvent`、`ToolCallEndEvent`、`FinalEvent`
- 前端可以看到模型"思考"的过程，以及每个工具调用的执行状态
- 支持 keepalive 注释防止代理超时

**生命周期**（`lifespan`）：
- 启动时：启动 Cron 服务、Reflection、空闲压缩、桌面宠物、QQ 通道
- 关闭时：优雅停止所有后台服务

#### 6. QQ Bot 通道（`claw/channels/qq.py`）

基于 QQ 官方 Bot API v2（WebSocket 网关）实现：
- 支持 C2C 私聊和群聊 @机器人
- 每个 QQ 对话自动绑定到独立 session
- 支持内联键盘按钮审批（通过 `qq_interactions.py`）
- 支持图片/媒体文件发送
- 支持斜杠命令（`/approve`、`/reject` 等）
- 凭证获取两种方式：扫码自动获取 或 手动在 QQ 开放平台创建

#### 7. 定时任务（`claw/scheduler/`）

- **CronService**：基于 croniter 的任务调度引擎，支持 cron 表达式、间隔、一次性定时
- **CronDispatcher**：定时触发时，为每个 job 创建 session 并调用 `run_agent_turn()`，结果投递回原通道
- **Heartbeat**：定期检查 workspace 中的 `HEARTBEAT.md`，监控活跃任务状态
- **持久化**：任务存储在 `data/cron/jobs.json`

#### 8. Skill 系统（`claw/skills/`）

- 每个 Skill 是 `skills/<name>/` 目录下的一个 `SKILL.md` 文件（含 YAML frontmatter）
- Skill 可包含 `assets/`（附件）和 `references/`（参考文档）子目录
- LLM 通过 `skills_list` 浏览、`skill_view` 加载、`skill_manage` 创建
- `use_skill` 工具触发审批（用户确认后 Skill 内容才注入 LLM 上下文）
- 支持 `always` 自动加载的 Skill

#### 9. 长期记忆（`claw/memory/`）

- 存储格式：Markdown 文件 + YAML frontmatter（metadata）
- 支持类别（category）、标签（tags）、重要性（importance 1-5）
- 版本追踪：每次修改递增版本号，ContextBuilder 缓存自动失效
- Reflection：每日定时运行，由 LLM 回顾近期对话并自动生成/更新记忆条目

#### 10. 桌面宠物（`claw/pet/`）

- 基于 tkinter 的桌面精灵窗口
- 支持多套精灵图（spritesheet），可通过 API 安装/切换/删除
- 用户自定义宠物存储在 `data/pets/`
- 独立子进程运行（通过 `PetProcessManager`），与 Gateway 解耦
- 通过 `PetStateBroker` 与 Gateway 通信（展示当前活动状态）

---

## 使用方式

### 命令速查

```bash
sjtuclaw chat      # 启动 CLI 交互对话
sjtuclaw gateway   # 启动 Gateway HTTP 服务（Web UI + REST API）
sjtuclaw setup     # 交互式配置向导（LLM + QQ Bot）
```

### CLI 斜杠命令

```text
/session new|list|switch|rename|delete  # 会话管理
/memory add|list|search|update|delete|stats  # 长期记忆管理
/compact                                # 手动压缩当前会话
/workspace set|show|unset               # 工作区管理
/workspace pick                         # 图形化选择文件夹
/cron list|status|disable|enable|delete # 定时任务管理
/approvals|approve|reject               # 审批操作
/reflect status|now|enable|disable     # 记忆反思
/auto on|off                            # 自动审批模式
/unlimited on|off                       # 无限模式（无 workspace 限制但需审批一切）
/pet open|close|settings               # 桌面宠物控制
/stop                                   # 终止当前任务
/exit                                   # 退出
/help                                   # 显示帮助
```

### Web UI

Gateway 启动后访问 `http://127.0.0.1:8000`：
- 多 Session 管理：创建/切换/重命名/删除会话
- 实时对话：Markdown 渲染 + 代码高亮 + KaTeX 数学公式
- 附件上传：拖拽上传图片/文件
- SSE 流式：实时显示 Agent 思考过程和工具执行状态
- 审批面板：工具调用确认、Skill 加载确认
- Prompt 热编辑：在线修改 system_prompt 和 soul
- 暗色模式：跟随系统或手动切换

### QQ Bot

在 `.env` 中配置 `QQ_ENABLED=true` 及 AppID/AppSecret 后，启动 Gateway 即自动连接：

```bash
# 扫码获取凭证
python -m claw.channels.qq_onboard

# 或在 .env 中手动填写
QQ_ENABLED=true
QQ_APP_ID=你的AppID
QQ_CLIENT_SECRET=你的AppSecret
QQ_ALLOW_FROM=*   # 或指定 OpenID
```

支持功能：
- C2C 私聊和群聊 @机器人
- 斜杠命令（`/approve`、`/reject` 等）
- 内联键盘按钮审批
- 媒体文件（图片等）自动转发

### Skill 系统

Skill 通过 LLM 工具驱动，无需记命令：

```text
# 对话中直接说需求，LLM 会自动查找合适的 Skill：
"帮我生成一份课程报告"

# LLM 自动调用 skills_list → 发现 course-report 匹配
# → 调用 use_skill → 用户审批 → 按指南生成报告

# 教 LLM 新技能：
"记住这个操作流程，下次直接复用"
# LLM 调用 skill_manage(action="create", ...) → 审批 → 保存为 Skill
```

### 定时任务

通过自然语言创建定时任务：

```text
"每天早上 9 点帮我整理对话摘要"
"每 30 分钟检查一次服务状态"
```

LLM 会调用 `cron` 工具自动创建，也可以通过 REST API 精确控制。

---

## 功能体系

### 已实现功能

| 类别 | 功能 | 状态 |
|------|------|------|
| **对话** | 多轮对话（Think→Act→Observe 循环） | ✅ |
| | 多 Session 管理（创建/切换/删除/重命名） | ✅ |
| | 自动 Session 标题生成 | ✅ |
| | SSE 流式事件推送 | ✅ |
| **上下文** | 上下文压缩（Compaction） | ✅ |
| | 空闲会话自动压缩 | ✅ |
| | Token 精确计数（tiktoken） | ✅ |
| | 独立压缩模型（降成本） | ✅ |
| | 摘要合并（增量压缩） | ✅ |
| **工具** | 只读：read_file, list_directory, search_files | ✅ |
| | 写：create_file, overwrite_file, edit_file | ✅ |
| | Shell：run_command | ✅ |
| | 联网：web_search (DuckDuckGo/Tavily), web_fetch | ✅ |
| | 下载：download, copy_attachment | ✅ |
| **安全** | 工具分级审批（read-only/write/shell） | ✅ |
| | Workspace 边界隔离 | ✅ |
| | 工作区外操作强制审批 | ✅ |
| | Gateway API Token 认证 | ✅ |
| | SSRF 防护（web_fetch） | ✅ |
| | 速率限制 | ✅ |
| | 上传大小限制 | ✅ |
| **记忆** | 长期记忆（Markdown + YAML） | ✅ |
| | 记忆检索（关键词/类别） | ✅ |
| | 每日反思（自动总结） | ✅ |
| | 记忆版本追踪 | ✅ |
| **通道** | CLI 交互 | ✅ |
| | Web UI（React SPA） | ✅ |
| | QQ Bot（C2C + 群聊） | ✅ |
| | QQ 内联键盘审批 | ✅ |
| | Gateway REST API | ✅ |
| **Skill** | Skill 注册与发现 | ✅ |
| | Skill 按需加载 | ✅ |
| | Skill 创建/修改（LLM 驱动） | ✅ |
| | Skill 使用统计 | ✅ |
| **调度** | Cron 定时任务 | ✅ |
| | 支持 cron 表达式/间隔/一次性 | ✅ |
| | Heartbeat 心跳监控 | ✅ |
| | 任务结果多通道投递 | ✅ |
| **宠物** | 桌面电子宠物（tkinter） | ✅ |
| | 多套精灵图 | ✅ |
| | 自定义宠物安装 | ✅ |
| | 活动状态展示 | ✅ |
| **管理** | System Prompt / Soul 热更新 | ✅ |
| | 定时任务 CRUD | ✅ |
| | 记忆统计与搜索 | ✅ |
| | 下载文件管理 | ✅ |

### 关键数字

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 上下文窗口 | 32000 tokens | `LLM_CONTEXT_WINDOW` |
| 上下文利用率 | 80% | `LLM_CONTEXT_USAGE_RATIO` |
| 最大迭代次数 | 15 | `CLAW_MAX_AGENT_ITERATIONS` |
| 单轮工具调用上限 | 20 | `CLAW_MAX_TOOL_CALLS_PER_TURN` |
| 压缩触发阈值 | 2000 tokens | `COMPACT_MAX_MESSAGE_TOKENS` |
| 空闲压缩 TTL | 60 分钟 | `COMPACT_IDLE_TTL_MINUTES` |
| 审批超时 | 300 秒 | 超时自动拒绝 |
| 心跳间隔 | 30 分钟 | `HEARTBEAT_INTERVAL_S` |
| 最大历史条目 | 2000 | `HISTORY_MAX_ENTRIES` |
| API 请求超时 | 120 秒 | `LLM_REQUEST_TIMEOUT` |

---

## API 路由表

### Chat
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/chat` | 发送消息，返回 Agent 回复 |
| POST | `/chat/stream` | SSE 流式聊天 |
| POST | `/stop` | 取消运行中的任务 |
| POST | `/command` | 执行斜杠命令 |

### Sessions
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/sessions` | 列出所有 Session |
| POST | `/sessions` | 创建 Session |
| GET | `/sessions/{id}/messages` | 获取消息列表 |
| PATCH | `/sessions/{id}` | 重命名 Session |
| DELETE | `/sessions/{id}` | 删除 Session |

### Attachments
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/sessions/{id}/attachments` | 上传附件 |
| GET | `/sessions/{id}/attachments` | 列出附件 |
| GET | `/sessions/{id}/attachments/{att_id}` | 获取附件内容 |
| GET | `/sessions/{id}/local-image` | 获取工作区本地图片 |

### Workspace
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/workspace` | 获取 workspace |
| POST | `/workspace` | 设置 workspace |
| DELETE | `/workspace` | 取消 workspace |
| POST | `/workspace/pick` | 图形化选择文件夹 |

### Approvals
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/approvals` | 列出待审批请求 |
| POST | `/approvals/{id}/approve` | 批准 |
| POST | `/approvals/{id}/reject` | 拒绝 |

### Cron
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/cron/jobs` | 列出定时任务 |
| POST | `/cron/jobs` | 创建定时任务 |
| GET | `/cron/jobs/{id}` | 获取任务详情 |
| DELETE | `/cron/jobs/{id}` | 删除任务 |
| POST | `/cron/jobs/{id}/enable` | 启用任务 |
| POST | `/cron/jobs/{id}/disable` | 禁用任务 |
| GET | `/cron/status` | Cron 服务状态 |

### Skills
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/skills` | 列出所有 Skill |
| GET | `/skills/{name}` | 获取 Skill 详情 |

### Memories
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/memories` | 列出记忆 |
| GET | `/memories/search?q=` | 搜索记忆 |
| GET | `/memories/stats` | 记忆统计 |
| POST | `/memories` | 添加记忆 |
| PATCH | `/memories/{id}` | 更新记忆 |
| DELETE | `/memories/{id}` | 删除记忆 |

### Reflection
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/reflect/config` | 获取反思配置 |
| PUT | `/reflect/config` | 更新反思配置 |
| POST | `/reflect/run` | 立即触发反思 |

### Pet
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/pet/settings` | 获取宠物设置 |
| PUT | `/pet/settings` | 更新宠物设置 |
| GET | `/pet/pets` | 列出可用宠物 |
| POST | `/pet/pets` | 安装自定义宠物 |
| DELETE | `/pet/pets/{id}` | 删除宠物 |
| GET | `/pet/pets/{id}/spritesheet` | 获取精灵图 |
| POST | `/pet/open` | 打开宠物窗口 |
| POST | `/pet/close` | 关闭宠物窗口 |
| GET | `/pet/state` | 获取宠物状态 |
| POST | `/pet/runtime/position` | 保存位置 |
| POST | `/pet/runtime/closed` | 窗口关闭通知 |

### Admin
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/system-prompt` | 获取 System Prompt |
| PUT | `/admin/system-prompt` | 更新 System Prompt |
| GET | `/admin/soul` | 获取 Soul |
| PUT | `/admin/soul` | 更新 Soul |

### Downloads / QQ
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/downloads` | 列出下载入口 |
| GET | `/downloads/{id}` | 获取下载文件 |
| GET | `/qq/status` | QQ 通道状态 |

---

## 配置说明

复制 `.env.example` 为 `.env` 后按需填写，**必填项仅 3 项**：

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_API_KEY` | API 密钥 | `sk-xxx` |
| `LLM_BASE_URL` | API 地址 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 模型名称 | `gpt-4o` |

所有可配置项的详细说明见 `.env.example` 中的注释，涵盖：
- 上下文窗口与利用率
- LLM 调用可靠性（重试/超时）
- 上下文压缩参数
- Agent Loop 行为
- 联网工具
- Gateway 网络与安全
- QQ Bot 凭证与权限
- 心跳监控

---

## 测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 运行特定测试
python -m pytest tests/test_core.py -v
python -m pytest tests/test_compaction.py -v
```

测试覆盖范围：核心功能、上下文压缩、Agent 工具回复、自动标题、取消轮次、Cron 集成、编码处理、Gateway 修复、宠物系统、QQ 媒体、记忆反思、安全加固、网络工具等。

---

## 技术栈

| 类别 | 技术 | 用途 |
|------|------|------|
| 语言 | Python 3.11+ | 主开发语言 |
| LLM | OpenAI 兼容 API | LLM 调用（支持 OpenAI / vLLM / Ollama / LM Studio） |
| Web 框架 | FastAPI + Uvicorn | Gateway HTTP Server |
| Token 计算 | tiktoken | 精确上下文 token 计数 |
| 异步网络 | aiohttp, httpx | QQ Bot WebSocket、HTTP 客户端 |
| 加密 | cryptography | QQ Bot 安全令牌 |
| 二维码 | qrcode | QQ 扫码登录 |
| 进程锁 | filelock | Cron 多进程安全 |
| 定时 | croniter | Cron 表达式解析 |
| 前端 | React 18 + TypeScript + Vite | Web UI SPA |
| 样式 | Tailwind CSS 3 | 前端样式 |
| Markdown | react-markdown + KaTeX | 消息渲染 + 数学公式 |
| 测试 | pytest + vitest | 后端/前端测试 |
| 桌面 | tkinter + Pillow | 桌面宠物 |
| 配置 | python-dotenv + PyYAML | 环境变量与 YAML 解析 |

---

## 注意事项

1. **API Key 安全**：`.env` 已在 `.gitignore` 中排除，切勿提交真实密钥。代码含自动脱敏逻辑。
2. **数据存储**：运行时数据在 `data/` 目录下，迁移环境时需保留此目录。
3. **Workspace 边界**：写文件和 Shell 命令只能在 workspace 内操作，通过 `/workspace set` 设置。
4. **审批超时**：默认 300 秒自动拒绝，操作不会执行。
5. **压缩失败保护**：上下文压缩失败时原始消息不会被删除，安全可逆。
6. **模型兼容性**：兼容所有支持 `/v1/chat/completions` 的服务（vLLM、Ollama、LM Studio 等）。
7. **QQ Bot**：群聊中只有发起操作的同一 QQ 用户可以审批工具调用。
8. **Web UI**：生产环境需先 `cd webui && npm run build` 构建前端静态文件。
