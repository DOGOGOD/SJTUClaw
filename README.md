# SJTUClaw

SJTUClaw 是一个最小 AI Agent Runtime，支持多轮对话、多 Session 管理、工具调用、审批流程、定时任务和 Skill 系统。

## 环境准备

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

依赖项：`openai>=1.0.0`, `python-dotenv>=1.0.0`, `fastapi>=0.100.0`, `uvicorn>=0.20.0`, `python-multipart>=0.0.5`

### 2. 配置 .env

在 `SJTUClaw/SJTUClaw/` 目录下复制 `.env.example` 为 `.env` 并填写：

```
LLM_API_KEY=你的API密钥
LLM_BASE_URL=模型服务地址
LLM_MODEL=模型名称
```

支持的 LLM 服务：任何兼容 OpenAI Chat Completions API 的服务（如 OpenAI、DeepSeek、本地 vLLM 等）。

> **警告**：`.env` 已在 `.gitignore` 中排除，**切勿**将真实 API Key 提交到 Git 仓库。

## 启动方式

所有命令均在 `SJTUClaw/SJTUClaw/` 目录下执行。

### CLI 模式（终端对话）

```bash
python -m claw.main
```

启动后进入交互对话界面，支持以下命令：

| 命令 | 说明 |
|------|------|
| `/exit` | 退出程序 |
| `/session new/list/switch/rename/delete` | Session 管理 |
| `/memory add/list/delete` | 长期记忆管理 |
| `/compact` | 手动触发对话压缩 |
| `/workspace set/show/unset` | 设置/查看工作目录 |
| `/approvals` | 查看待审批操作 |
| `/approve <id>` | 批准 |
| `/reject <id> [原因]` | 拒绝 |
| `/skill list/show/<name>` | 查看可用 Skill |
| `/skill <name> <task>` | 显式调用 Skill |
| `/skill usage` | 查看 Skill 使用记录 |

### Gateway + WebUI 模式

```bash
python -m claw.gateway
```

浏览器打开 **`http://127.0.0.1:8000`** 即可使用完整图形界面。

WebUI 功能：对话、Session 管理、附件上传、Workspace 设置、审批操作、下载文件、Skill 选择、定时任务管理。

### 运行自动化测试

```bash
# pytest 单元测试（30 项）
python -m pytest tests/test_core.py -v

# Step 8 综合自检（44 项）
python -m tests.test_step8_selfcheck

# Step 9 综合自检（38 项）
python -m tests.test_step9_selfcheck
```

## 使用流程示例

### 1. 设置 Workspace

```
User> /workspace set C:\Users\me\my-project
Workspace 已设置为: C:\Users\me\my-project
```

所有文件修改、shell 命令、下载入口创建都会被限制在此目录内。

### 2. 对话中使用工具

```
User> 帮我创建一个 hello.py 文件，内容是打印 Hello World
[tool_call #1] create_file {"path": "hello.py"}
[审批] 模型请求执行: create_file
  输入 /approve apr_xxx 批准
Approval> /approve apr_xxx
已批准: [apr_xxx] create_file
[tool_result] create_file: 成功
Assistant> 已创建 hello.py 文件。
```

### 3. 使用 Skill

```
# 显式调用
User> /skill course-report 写一份2000字的读书报告，主题是AI发展史，保存为 report.md

# 或让模型自主选择（不加 /skill 前缀）
User> 帮我整理 workspace 里这些笔记，生成一份知识总结
```

### 4. 使用 Shell

```
User> /workspace set C:\my-project
User> 帮我运行测试
[tool_call] new_shell {}
Approval> /approve ...
[tool_call] run_command {"command": "python -m pytest"}
Approval> /approve ...
```

## 项目结构

```
SJTUClaw/
├── README.md
├── docs/
│   ├── ARCHITECTURE.md      # 架构说明
│   ├── DEVELOPMENT_PLAN.md  # 开发计划
│   └── QA_CHECKLIST.md      # 验收清单
├── prompts/
│   ├── system_prompt.md     # 系统规则
│   └── soul.md              # Agent 风格
├── skills/
│   ├── course-report/       # 课程报告生成
│   ├── material-summary/    # 材料汇总
│   └── presentation-outline/# 展示大纲
├── web/
│   └── index.html           # WebUI 前端
├── data/                    # 运行产物（gitignore）
├── claw/                    # 主 Python 包
│   ├── main.py              # CLI 入口
│   ├── config.py            # 配置加载
│   ├── agent/loop.py        # Agent 核心循环
│   ├── context/builder.py   # 上下文组装
│   ├── tools/               # 工具系统
│   ├── workspace/           # 工作区管理
│   ├── approval/            # 审批系统
│   ├── skills/              # Skill 注册
│   ├── gateway/server.py    # HTTP 服务
│   └── scheduler/           # 定时任务
└── tests/                   # 测试
```
