# SJTUClaw — Minimal AI Agent Runtime

SJTUClaw 是一个面向教学的渐进式 AI Agent 运行环境。它从最基础的 LLM API 调用出发，逐步实现多轮对话、多 Session 管理、上下文压缩（Compaction）、工具调用（Tool Calling）、Gateway 服务层、QQ 机器人通道、定时任务调度、Workspace 管理、高级工具与审批系统，最终形成一套完整的 Skill 系统。

项目以 Python 实现，采用 FastAPI 构建 Gateway，原生支持 OpenAI 兼容的 function calling，提供 Web UI 和 QQ Bot 两种图形化交互入口。

> 📄 完整的功能清单、模块详解、数据结构设计与后续开发计划请参阅 [docs/中期报告.md](docs/中期报告.md)。

---

## 快速开始

### 环境要求

- Python >= 3.11

### 安装

```bash
# 克隆仓库
cd SJTUClaw

# 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# 安装依赖
pip install -r requirements.txt

# 注册 sjtuclaw 命令（pip 可编辑安装）
pip install -e .
```

这样安装后，`sjtuclaw` 命令即可在任意路径下使用。

### 配置

```bash
# 方式一：交互式配置向导（推荐，安装后可一键完成 LLM + QQ Bot 配置）
sjtuclaw setup

# 方式二：手动复制模板并编辑
cp .env.example .env
# 编辑 .env，至少填写以下三项：
# LLM_API_KEY=sk-your-api-key-here
# LLM_BASE_URL=https://api.openai.com/v1
# LLM_MODEL=gpt-4o
```

### 运行

```bash
# ---- sjtuclaw 命令（推荐，pip install -e . 后可用）----
sjtuclaw chat                  # 启动 CLI 交互对话（默认命令）
sjtuclaw gateway               # 启动 HTTP Gateway + Web UI + REST API
sjtuclaw setup                 # 交互式配置向导（LLM + QQ Bot 一键配置）

# ---- 或通过 Python 模块方式启动（无需安装）----
python -m claw.main            # 等同于 sjtuclaw chat
python -m claw.gateway         # 等同于 sjtuclaw gateway
# 访问 http://127.0.0.1:8000
```

---

## 项目结构

```
SJTUClaw/
├── claw/                          # 核心代码包
│   ├── main.py                    # CLI 入口，组装所有组件
│   ├── config.py                  # 配置加载（.env / 环境变量）
│   ├── agent/                     # Agent 运行时（Loop、预算、事件、指标、健康监控）
│   ├── llm/                       # LLM API 层（客户端、协议解析、重试机制）
│   ├── session/                   # Session 管理（数据模型、JSONL 持久化、自动标题）
│   ├── context/                   # 上下文构造（组装、压缩、治理、token 计数）
│   ├── memory/                    # 长期记忆（Markdown 文件 + YAML frontmatter、每日反思）
│   ├── tools/                     # 工具系统（注册、校验、只读/写/Shell/下载/附件/记忆/Skill 工具）
│   ├── gateway/                   # HTTP 网关（FastAPI 路由、SSE 流、中间件限流）
│   ├── channels/                  # 消息平台通道（QQ Bot WebSocket 实现）
│   ├── scheduler/                 # 定时任务调度（Cron 服务、心跳监测）
│   ├── skills/                    # Skill 系统（扫描注册、使用统计）
│   ├── approval/                  # 审批系统（线程安全、超时自动拒绝）
│   ├── workspace/                 # 工作区管理（per-session 绑定、边界检查）
│   ├── cli/                       # 命令行界面（REPL、命令处理）
│   └── prompts/                   # 提示模板（system_prompt.md、soul.md、tool_contract.md）
├── skills/                        # Skill 数据目录（每个子目录为一个 Skill）
├── web/                           # Web UI（SPA 静态文件）
├── webui/                         # Web UI 源码（npm run build 构建）
├── docs/                          # 文档（中期报告、报告撰写指南）
├── tests/                         # 测试文件
├── data/                          # 运行时数据（自动生成，已 gitignore）
│   ├── sessions/                  # Session JSONL 文件
│   ├── memory/                    # 记忆 Markdown 文件
│   └── cron/                      # 定时任务存储
├── pyproject.toml                 # 项目元数据与构建配置
├── requirements.txt               # 依赖清单
└── .env.example                   # 环境变量模板
```

### 核心架构

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

---

## 使用方式

### 命令速查

```bash
sjtuclaw chat      # 启动 CLI 交互对话
sjtuclaw gateway   # 启动 Gateway HTTP 服务（Web UI + API）
sjtuclaw setup     # 交互式配置向导（LLM + QQ Bot）
```

### 交互式配置向导

`sjtuclaw setup` 通过问答方式完成初始化配置：

1. **LLM 配置** — API Key、Base URL、Model（已有配置显示当前值，可选跳过）
2. **QQ Bot 配置** — 支持扫码自动获取凭证，或手动输入 AppID/AppSecret

### CLI 斜杠命令

```text
/session new|list|switch|rename|delete  # 会话管理
/memory add|list|search|delete|stats    # 长期记忆管理
/compact                                # 手动压缩当前会话
/workspace set|show|unset               # 工作区管理
/cron list|status|disable|enable|delete # 定时任务管理
/approvals|approve|reject               # 审批操作
/reflect status|now|enable|disable      # 记忆反思
/auto on|off                            # 自动审批模式
/stop                                   # 终止当前任务
/help                                   # 显示帮助
/exit                                   # 退出
```

### QQ Bot

在 `.env` 中配置 `QQ_ENABLED=true` 及 AppID/AppSecret 后，启动 Gateway 即自动连接 QQ WebSocket，支持 C2C 私聊和群聊 @机器人。

### 定时任务

通过 LLM 自然语言创建："帮我每天早上 9 点整理对话摘要"，或通过 REST API 精确控制。

### Skill 系统

Skill 通过 LLM 工具驱动，无需记命令。LLM 自主调用 `skills_list` 浏览可用 Skill、`skill_view` 加载完整指南、`skill_manage` 创建或修改 Skill：

```text
# 对话中直接说需求，LLM 会自动查找合适的 Skill：
"帮我生成一份课程报告"

# LLM 自动调用 skills_list → 发现 course-report 匹配
# → 调用 skill_view("course-report") → 按指南生成报告

# 教 LLM 新技能：
"记住这个操作流程，下次直接复用"
# LLM 调用 skill_manage(action="create", ...) → 审批 → 保存为 Skill
```

---

## 配置说明

复制 `.env.example` 为 `.env` 后按需填写，必填项仅 3 项：

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_API_KEY` | API 密钥 | `sk-xxx` |
| `LLM_BASE_URL` | API 地址 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 模型名称 | `gpt-4o` |

完整配置项说明（上下文窗口、压缩参数、Agent Loop 调优、Gateway、QQ Bot、代理等）见 `.env.example` 中注释。

---

## 核心技术栈

| 类别 | 技术 | 用途 |
|------|------|------|
| 语言 | Python 3.11+ | 主开发语言 |
| LLM | OpenAI 兼容 API | LLM 调用 |
| Web 框架 | FastAPI + Uvicorn | Gateway HTTP Server |
| Token 计算 | tiktoken | 精确上下文 token 计数 |
| 异步网络 | aiohttp, httpx | QQ Bot WebSocket |
| 加密 | cryptography | QQ Bot 安全令牌 |
| 二维码 | qrcode | QQ 扫码登录 |
| 进程锁 | filelock | Cron 多进程安全 |
| 定时 | croniter | Cron 表达式解析 |

---

## 注意事项

1. **API Key 安全**：`.env` 已在 `.gitignore` 中排除，切勿提交真实密钥。代码含自动脱敏逻辑，但仍需检查日志。
2. **数据存储**：运行时数据在 `data/` 目录下，迁移环境时需保留此目录。
3. **Workspace 边界**：写文件和 Shell 命令只能在 workspace 内操作，通过 `/workspace set` 设置。
4. **审批超时**：默认 300 秒自动拒绝，操作不会执行。
5. **压缩失败保护**：上下文压缩失败时原始消息不会被删除，安全可逆。
6. **模型兼容性**：兼容所有支持 `/v1/chat/completions` 的服务（vLLM、Ollama、LM Studio 等）。
