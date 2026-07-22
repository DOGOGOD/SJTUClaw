# SJTUClaw

面向个人与教学场景的本地 AI Agent Runtime。

SJTUClaw 将多轮对话、工具调用、长期记忆、Skill、定时任务和桌面宠物整合为一个可扩展的 Agent 工作台。项目提供 Windows 桌面应用、CLI、Web UI、REST API 和 QQ Bot 多种入口，适合学习 Agent Runtime，也适合搭建个人自动化助手。

## 界面预览

![SJTUClaw Web UI 首页](docs/images/readme-webui.png)

![SJTUClaw 设置界面](docs/images/readme-settings.png)

![SJTUClaw 宠物功能设置](docs/images/readme-pet.png)

## 核心功能

- **统一 Agent Loop**：CLI、Web UI、QQ Bot、Heartbeat 和 Cron 共享 `run_agent_turn()`。
- **可选 Pi Agent 后端**：通过官方 JSONL RPC 接入 Pi，保留其模型提供商、工具循环、Skills、Extensions、自动压缩、重试和持久会话，同时沿用 SJTUClaw 的界面、渠道与审批体验。
- **工具调用与安全审批**：支持文件读写、Shell、联网、下载、记忆、Skill 和 Cron 工具，并按安全级别控制执行。
- **可控执行模式**：提供按 Session 隔离的 AUTO 与 UNLIMITED 模式，可在自动执行效率和文件系统安全边界之间明确切换。
- **上下文与长期记忆**：支持 Session 持久化、上下文压缩、Markdown 记忆和每日 Reflection。
- **Workspace 回退**：设置 workspace 后自动创建逐回合检查点，同时恢复文件和对话；未设置 workspace 时保持关闭。
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
│   ├── workspace/                # Workspace 绑定、边界检查、SQLite + CAS 回退
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
%USERPROFILE%\.sjtuclaw\data
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

需要使用 Pi 时，先构建同级目录中的 `pi` 仓库，或在系统中安装可执行的 `pi`；随后在 Web UI 的“设置 → LLM”把 Agent backend 切换为 Pi。Pi 可以直接复用已有的 OpenAI-compatible 模型配置，也可以通过 `PI_PROVIDER`、`PI_MODEL` 使用 Pi 自身的模型与认证配置。

完整配置项、时区覆盖方式和安全建议见 [配置说明](docs/configuration.md)。

#### 启动

```bash
sjtuclaw chat       # CLI 交互对话
sjtuclaw gateway    # Gateway、Web UI 与 REST API
sjtuclaw-desktop    # Desktop：本地 Gateway + pywebview 独立窗口
```

Gateway 启动后访问 <http://127.0.0.1:8000>。

前端开发：

```bash
cd webui
npm install
npm run dev         # http://127.0.0.1:5173
```

源码方式默认以项目根目录作为 Agent 主目录，并在项目内使用 `data/`、`prompts/` 和 `skills/`；安装版则以当前用户主目录下的 `.sjtuclaw` 作为 Agent 主目录，并使用 `.sjtuclaw/data`（Windows 示例：`C:\Users\<用户名>\.sjtuclaw\data`）保存可写数据。日志、环境配置与运行时文件也统一位于 `.sjtuclaw`。两种方式共用同一套 Agent、Tool、Memory、Skill、Scheduler、Workspace 回退和 Gateway 代码，主要区别在启动入口、资源路径和运行数据位置。

`requirements.txt` 提供可复现的完整 Python 开发环境，包含核心运行时、Desktop、PyInstaller 和 pytest。若只希望安装最小核心运行时，可改用 `python -m pip install -e .`；只添加 Desktop 支持可使用 `python -m pip install -e ".[desktop]"`。

### 桌面宠物与自定义宠物包

在 Web UI 的“设置 → Pet”中可以开启或关闭桌面宠物、选择角色、设置是否随 Gateway 启动，以及导入或删除自定义宠物。自定义宠物必须以 ZIP 压缩包导入，包内只能包含 `pet.json` 和一张 `spritesheet.webp` 或 `spritesheet.png`。两个文件可以直接位于 ZIP 根目录，也可以放在一个与宠物 ID 同名的顶层目录中：

```text
shin-chan.zip
└── shin-chan/                 # 这一层可以省略
    ├── pet.json
    └── spritesheet.webp
```

`pet.json` 示例：

```json
{
  "id": "shin-chan",
  "displayName": "蜡笔小新",
  "description": "陪伴你完成任务的桌面宠物。",
  "spriteVersionNumber": 2,
  "spritesheetPath": "spritesheet.webp"
}
```

字段要求：

- `id`：1–64 个字符，只允许小写字母 `a-z`、数字 `0-9`、下划线 `_` 和短横线 `-`，不能使用 Windows 系统保留名称；例如应写成 `shin-chan`，不能写成 `Shin-chan`。
- `displayName`：界面显示名称，可以使用中文，不能为空，最长 100 个字符。
- `description`：可选说明，最长 1000 个字符。它也是 LLM 生成宠物互动台词时的角色设定，建议写清性格、口吻、自称和与用户的关系。
- `spriteVersionNumber`：必须是数字 `1` 或 `2`，并与图集尺寸匹配。
- `spritesheetPath`：必须是 `spritesheet.webp` 或 `spritesheet.png`。

| 图集版本 | 布局 | 固定尺寸 | 功能 |
|---------|------|----------|------|
| v1 | 8 列 × 9 行 | 1536 × 1872 | 9 行基础动画；保留用于兼容现有宠物 |
| v2 | 8 列 × 11 行 | 1536 × 2288 | 包含全部基础动画，并增加两行共 16 个观察方向 |

目前内置的“月薪喵”和“线条小狗”均为 v1。新制作的宠物建议使用 v2；`spriteVersionNumber` 必须写成 JSON 数字，例如 `2`，不能写成字符串 `"2"`。

导入时，Gateway 会在写入用户宠物目录前检查 ZIP 完整性、路径安全、重复或额外文件、加密与异常压缩比、压缩包大小、`pet.json` 字段、图片真实格式、透明通道、图集尺寸、必用动画帧以及未使用格子的透明性。校验失败时，具体原因会直接显示在添加宠物弹窗中。相同 ID 的宠物不能重复安装，自定义宠物也不能覆盖内置宠物。

宠物导入成功后，Gateway 会调用当前配置的 LLM，根据 `displayName` 和 `description` 生成 12 条符合角色人设的“被点击/轻戳”回复。回复不会写入宠物压缩包，而是按宠物 ID 独立保存到 `data/pet/replies/<pet-id>.json`；桌面宠物被点击时，只会从当前宠物自己的回复中随机选择一句。按 ID 分文件的结构也便于后续删除宠物时同步清理其台词，而不影响其他宠物。

如果尚未配置 LLM，或者模型调用失败、返回格式不正确，导入仍会成功，系统会为该宠物单独保存通用备用台词，并在 Web UI 中显示提示。之后重新导入同一宠物前仍需先删除原宠物；当前版本不会自动重新生成已经保存的备用台词。

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

### Workspace 回退

为 Session 设置 workspace 后，系统会自动在每次用户消息执行前创建检查点；未设置 workspace 时不启用回退。Web UI 会在可回退的用户消息下显示返回箭头，也可以在 Web UI 或 CLI 使用：

```text
/rollback                 # 回退一轮
/rollback 3               # 回退到倒数第 3 个用户回合之前
/rollback <checkpointId>  # 回退到指定检查点
/rollback list            # 列出可用检查点
/rollback status          # 查看状态
/rollback undo            # 撤销最近一次回退
```

一次回退会原子性地恢复 workspace 文件和对应的完整对话状态。文件内容按 SHA-256 去重保存在独立对象库中，元数据、会话快照和操作日志保存在 SQLite；不会修改或依赖 workspace 中的 Git 仓库。上下文 compact 只推进摘要边界，不删除原始消息，摘要和边界会随 Session 一起持久化；后台 compact 结果还会校验 Session revision，因此不会覆盖回退后的状态。

`/rollback undo` 是单步撤销：回退后如果开始了新的用户回合，旧 undo 安全点会自动失效。会话快照会压缩存储；分支失效、切换或取消 workspace 时，系统会清理不可达检查点并对内容对象执行引用扫描回收。

回退只覆盖已绑定的 workspace。开启 UNLIMITED 后发生在 workspace 外的改动不会被恢复，预览和执行结果会明确提示这一点。切换或取消 workspace 会使旧绑定的检查点失效。

### 构建 Windows 安装包

准备 Python 3.11+、Node.js 18+ 和 Inno Setup 7 后，在项目根目录执行：

```powershell
.\packaging\windows\build.ps1
```

构建脚本会先安装依赖、构建 WebUI、检查 Tkinter，再运行 PyInstaller；它会从 `PATH` 和常见安装目录自动查找 Inno Setup。找不到 Inno Setup 时仍会保留可运行的 PyInstaller 目录版，也可以使用 `-SkipInstaller` 主动跳过安装向导。

> 修改 Python 或 WebUI 源码后必须重新运行构建脚本。`dist/` 中已有的 EXE 和安装包不会自动包含最新源码。

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

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端 | Python 3.11、FastAPI、Uvicorn |
| LLM | OpenAI 兼容 API、httpx、aiohttp |
| Agent | 自研 Agent Loop、ToolRegistry、上下文压缩、审批管理 |
| 存储 | JSONL Session、SQLite 回退元数据、SHA-256 对象库、Markdown + YAML 记忆 |
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
