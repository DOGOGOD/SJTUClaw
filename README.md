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
- **可控执行模式**：提供按 Session 隔离的 AUTO 与 UNLIMITED 模式，可在自动执行效率和文件系统安全边界之间明确切换。
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
├── claw/                         # Python 主程序
│   ├── agent/                    # Agent Loop、预算、事件、健康监控
│   ├── approval/                 # 高风险工具审批管理
│   ├── channels/                 # 外部渠道，目前以 QQ Bot 为主
│   ├── cli/                      # CLI 入口、REPL、命令解析
│   ├── context/                  # Context Builder、Compact、治理与 Token 预算
│   ├── gateway/                  # FastAPI Gateway、REST API、SSE、上传服务
│   ├── llm/                      # OpenAI Compatible 客户端与协议适配
│   ├── memory/                   # 长期记忆存储与 Reflection
│   ├── pet/                      # 桌面宠物进程与资源管理
│   ├── prompts/                  # Prompt 模板加载
│   ├── scheduler/                # Cron、Heartbeat、任务分发与状态持久化
│   ├── session/                  # Session/Message 模型、标题与 JSONL Store
│   ├── skills/                   # Skill Registry、安装、统计与状态管理
│   ├── tools/                    # 文件、Shell、网页、附件、Memory、Cron、Skill 等工具
│   ├── workspace/                # Workspace 绑定、路径解析、边界检查
│   ├── config.py                 # 配置加载与运行时入口配置
│   ├── runtime_settings.py       # Web UI 可写设置与敏感配置持久化
│   ├── desktop.py                # Windows 桌面壳，启动本地 Gateway 与 pywebview
│   ├── paths.py                  # 源码版、PyInstaller 版、安装版路径切换
│   ├── main.py                   # 应用主入口
│   └── utils.py                  # 通用工具函数
├── prompts/                      # identity、system prompt、soul、tool contract 等文本资源
├── skills/                       # 内置 Skill 目录
│   ├── course-report/
│   ├── material-summary/
│   └── presentation-outline/
├── webui/                        # React + TypeScript + Vite 前端源码
│   ├── src/
│   │   ├── components/           # 线程视图、设置面板、通用 UI 组件
│   │   ├── hooks/                # 会话、主题、拖拽等前端 hooks
│   │   ├── i18n/                 # 国际化文案与语言资源
│   │   ├── lib/                  # API 客户端、类型、命令与工具函数
│   │   ├── providers/            # React provider 封装
│   │   ├── test/                 # 前端测试辅助
│   │   ├── types/                # 前端类型定义
│   │   ├── globals.css
│   │   └── main.tsx
│   ├── public/                   # 前端静态资源与宠物图片
│   ├── package.json
│   └── vite.config.ts
├── web/                          # 已构建的 Web UI 静态产物，供 Gateway/桌面版直接加载
├── packaging/
│   └── windows/
│       ├── build.ps1             # 一键构建脚本
│       ├── SJTUClaw.spec         # PyInstaller 打包规格
│       ├── SJTUClaw.iss          # Inno Setup 安装脚本
│       └── assets/SJTUClaw.ico   # Windows 程序与快捷方式图标
├── docs/
│   ├── configuration.md          # 配置说明
│   ├── testing.md                # 测试与开发说明
│   ├── windows-packaging.md      # Windows 安装包构建说明
│   └── images/                   # README 与文档截图
├── tests/                        # pytest 后端测试与少量前端/集成测试
├── data/                         # 源码运行时数据目录
├── build/                        # 本地构建中间产物
├── dist/                         # PyInstaller 与安装包输出目录
├── requirements.txt              # Python 依赖列表
├── pyproject.toml                # Python 项目元数据与 `sjtuclaw` CLI 入口
├── .env.example                  # 环境变量模板
├── SJTUClaw.md                   # 课程任务说明
└── 中期报告.md                    # 当前阶段报告
```

结构说明：

- `claw/` 是核心运行时，桌面端、CLI、Web、QQ 和调度器最终都会汇入同一套 Agent Loop。
- `webui/` 是完整前端工程，开发时由 Vite 提供热更新，发布时构建到 `web/`。
- `packaging/windows/` 负责 Windows 桌面端分发，先用 PyInstaller 冻结 Python 程序，再用 Inno Setup 生成标准安装包。
- `prompts/`、`skills/` 和 `data/` 分别对应静态 Prompt 资源、内置 Skill 资源和源码运行时的可写数据。
- `docs/` 放配置、测试和打包文档；`中期报告.md` 是课程阶段性说明，内容会比 README 更简洁。

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

### AUTO 与 UNLIMITED 模式

SJTUClaw 默认对写入和 Shell 等高风险工具进行审批，并将文件及命令操作限制在当前 Session 绑定的 workspace 内。AUTO 和 UNLIMITED 是两个相互独立、按 Session 生效的执行模式；新建 Session 时二者默认均为关闭状态，Gateway 重启后也会恢复为关闭状态。

| 模式 | 作用 | 审批行为 | 文件系统边界 |
|------|------|----------|--------------|
| 默认模式 | 使用完整安全保护 | 写入和 Shell 操作逐次审批 | 仅允许访问当前 workspace |
| AUTO | 减少 workspace 内操作的人工确认 | 自动批准写入和 Shell 操作；Skill 加载确认仍保留 | 仍严格限制在当前 workspace，越界操作由工具拒绝 |
| UNLIMITED | 解除 workspace 路径限制 | 写入、覆盖、删除和 Shell 操作始终逐次审批，AUTO 无法跳过 | 可访问 workspace 外路径 |

启用或查看 AUTO 模式：

```text
/auto on       # 开启
/auto off      # 关闭
/auto toggle   # 切换
/auto status   # 查看当前 Session 的状态
```

启用或查看 UNLIMITED 模式：

```text
/unlimited on       # 允许访问 workspace 外路径
/unlimited off      # 恢复 workspace 边界
/unlimited toggle   # 切换
/unlimited status   # 查看当前 Session 的状态
```

> AUTO 不等于取消安全边界：它只省略 workspace 沙箱内写入和 Shell 操作的逐次审批。UNLIMITED 才会解除路径边界，但不会取消危险操作审批。两个模式同时开启时，UNLIMITED 的强制审批规则优先。

### 构建 Windows 安装包

准备 Python 3.11+、Node.js 18+ 和 Inno Setup 7 后，在项目根目录执行：

```powershell
.\packaging\windows\build.ps1
```
需要将build.ps1中的Inno Setup路径修改为本地真实路径

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
/auto on|off|toggle|status
/unlimited on|off|toggle|status
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
