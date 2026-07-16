# SJTUClaw

面向个人与教学场景的本地 AI Agent Runtime。

SJTUClaw 将多轮对话、工具调用、长期记忆、Skill、定时任务和桌面宠物整合为一个可扩展的 Agent 工作台。项目提供 Windows 桌面应用、CLI、Web UI、REST API 和 QQ Bot 多种入口，适合学习 Agent Runtime，也适合搭建个人自动化助手。

## 界面预览

![SJTUClaw Web UI 首页](docs/images/readme-webui.png)

![SJTUClaw 设置界面](docs/images/readme-settings.png)

![SJTUClaw 宠物功能设置](docs/images/readme-pet.png)

## 核心功能

- **统一 Agent Loop**：CLI、Web UI、QQ Bot、Heartbeat 和 Cron 共享 `run_agent_turn()`。
- **工具调用与安全审批**：支持文件读写、Shell、联网、下载、记忆、Skill 和 Cron 工具，并按安全级别控制执行。
- **上下文与长期记忆**：支持 Session 持久化、上下文压缩、Markdown 记忆和每日 Reflection。
- **Skill 系统**：通过 `SKILL.md` 组织可复用工作流，支持发现、加载和管理。
- **多入口与实时反馈**：Web UI 通过 SSE 展示 Agent 事件，QQ Bot 支持私聊、群聊和内联审批。
- **本地化时间与定时任务**：自动识别系统时区，支持 `CLAW_TIMEZONE` 显式覆盖，识别失败时回退到上海时区。
- **Windows 桌面应用**：使用 pywebview 承载完整 Web UI，通过 PyInstaller 打包后无需单独安装 Python 或 Node.js。
- **标准安装与卸载体验**：使用 Inno Setup 7 生成安装向导，支持自选安装路径、开始菜单、桌面快捷方式、覆盖升级和系统卸载入口。
- **桌面宠物**：支持角色选择、独立窗口、状态展示和随 Gateway 启动。

## 项目结构

```text
SJTUClaw/
├── claw/
│   ├── agent/          # Agent Loop、事件和运行时预算
│   ├── context/        # 上下文构造与压缩
│   ├── llm/            # OpenAI 兼容的 LLM 客户端
│   ├── session/        # 会话模型与 JSONL 持久化
│   ├── memory/         # 长期记忆与每日反思
│   ├── tools/          # 文件、Shell、联网、Skill、Cron 等工具
│   ├── gateway/        # FastAPI Gateway、REST API、SSE
│   ├── channels/       # QQ Bot 通道
│   ├── scheduler/      # Cron 与 Heartbeat 调度
│   ├── skills/         # Skill 注册与使用统计
│   ├── pet/            # tkinter 桌面宠物
│   ├── desktop.py      # Windows 桌面应用启动器
│   ├── paths.py        # 源码版与安装版资源/数据路径
│   └── cli/            # CLI 与 REPL
├── prompts/            # System Prompt 与 Soul
├── skills/             # 可复用 Skill 数据目录
├── webui/              # React + TypeScript 前端源码
├── web/                # Web UI 构建产物
├── packaging/windows/  # PyInstaller、Inno Setup 与应用图标
├── tests/              # 后端与前端测试
├── data/               # 运行时数据，默认不提交
├── docs/               # 配置、测试和项目文档
├── pyproject.toml      # Python 项目与 CLI 配置
└── .env.example        # 环境变量模板
```

## 使用方式

### Windows 桌面版

运行发布目录中的 `SJTUClaw-Setup-0.1.0.exe`，按照安装向导选择安装位置和是否创建桌面快捷方式。安装后可从开始菜单或桌面启动 SJTUClaw，程序会自动启动本地 Gateway，并在独立桌面窗口中打开完整 Web UI。

安装版的可写数据默认保存在：

```text
%APPDATA%\SJTUClaw\data
```

其中包括会话、记忆、模型设置、定时任务、用户 Skill 和宠物资源。重新安装或覆盖升级不会主动删除这些用户数据。卸载可通过 Windows“已安装的应用”、开始菜单中的“卸载 SJTUClaw”，或安装目录内的卸载程序完成。

> 安装包适用于 64 位 Windows。首次使用仍需在设置界面配置可用的 OpenAI Compatible 模型服务。

### 源码运行

#### 环境要求

- Python 3.11+
- Node.js 18+（仅前端开发或重新构建 Web UI 时需要）
- OpenAI 兼容的模型服务，例如 OpenAI、Ollama、vLLM 或 LM Studio

#### 安装与配置

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install -e .
sjtuclaw setup
```

也可以复制 `.env.example` 为 `.env` 手动配置模型服务。

完整配置项、时区覆盖方式和安全建议见 [配置说明](docs/configuration.md)。

#### 启动

```bash
sjtuclaw chat       # CLI 交互对话
sjtuclaw gateway    # Gateway、Web UI 与 REST API
```

Gateway 启动后访问 <http://127.0.0.1:8000>。

前端开发：

```bash
cd webui
npm install
npm run dev         # http://127.0.0.1:5173
```

源码方式默认在项目目录下使用 `data/`、`prompts/` 和 `skills/`；安装版则使用 `%APPDATA%\SJTUClaw\data` 保存可写数据。两种方式共用同一套 Agent、Tool、Memory、Skill、Scheduler 和 Gateway 代码，主要区别在启动入口、资源路径和运行数据位置。

### 构建 Windows 安装包

准备 Python 3.11+、Node.js 18+ 和 Inno Setup 7 后，在项目根目录执行：

```powershell
.\packaging\windows\build.ps1
```

构建产物：

```text
dist\SJTUClaw\SJTUClaw.exe
dist\installer\SJTUClaw-Setup-0.1.0.exe
```

详细说明见 [Windows 安装包构建](docs/windows-packaging.md)。

### 常用操作

```text
/session new|list|switch|rename|delete
/workspace set|show|unset
/cron list|status|disable|enable|delete
/approvals|approve|reject
/pet open|close|settings
/help
```

也可以直接用自然语言创建定时任务、保存记忆或请求使用 Skill。

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端 | Python 3.11、FastAPI、Uvicorn |
| LLM | OpenAI 兼容 API、httpx、aiohttp |
| Agent | 自研 Agent Loop、ToolRegistry、上下文压缩、审批管理 |
| 存储 | JSONL Session、Markdown + YAML 记忆、文件系统运行时数据 |
| 调度 | croniter、Heartbeat |
| 前端 | React 18、TypeScript、Vite、Tailwind CSS |
| 渲染 | react-markdown、KaTeX、代码高亮 |
| 通道 | Windows 桌面应用、CLI、Web UI、REST API、QQ Bot WebSocket |
| 桌面 | pywebview、PyInstaller、Inno Setup 7、tkinter、Pillow |
| 测试 | pytest、Vitest |

## 文档

- [配置说明](docs/configuration.md)
- [测试与开发](docs/testing.md)
- [Windows 安装包构建](docs/windows-packaging.md)
- [前端源码](webui/)
- [Skill 目录](skills/)
