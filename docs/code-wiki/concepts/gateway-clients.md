# Gateway and Clients

> Gateway 把共享 Agent Runtime 暴露为 REST、SSE 和静态 Web 应用；Desktop、Web UI、TUI、CLI、QQ 和桌宠只是不同的交互适配器。

## Gateway 启动

入口：

```text
sjtuclaw gateway
python -m claw.gateway
claw.gateway.server:app
```

`claw/gateway/__main__.py` 读取 Host、Port 和自动打开浏览器配置。监听非回环地址但未设置 `GATEWAY_API_TOKEN` 时拒绝启动。

FastAPI `lifespan` 启动：

- 下载注册表
- CronService
- ReflectionManager
- 按设置启动桌宠
- 按设置启动 QQ Channel

关闭时：

- 停止 QQ
- 等待当前压缩线程
- 停止 Reflection 与 Cron
- 关闭全部 microVM
- 停止桌宠进程

## 中间件

注册顺序由 FastAPI 包装规则决定，功能包括：

| 中间件 | 作用 |
| --- | --- |
| CORS | 只允许本机默认 Origin 和显式配置 Origin |
| RequestSize | 普通请求 10 MB，附件和宠物包约 51 MB |
| RateLimit | 每客户端滑动窗口 300 次 / 分钟 |
| GatewaySecurity | Origin 校验和远程 Token |
| RequestLogging | 请求 ID、状态、耗时和慢请求日志 |

远程 Token 可以放在：

```text
Authorization: Bearer <token>
X-SJTUClaw-Token: <token>
```

静态首页与 Assets 是只读资源；能读取或修改 Agent 状态的 API 仍经过安全检查。

## API 分组

### 对话与命令

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/chat` | 同步完成一个 Agent Turn |
| POST | `/chat/stream` | SSE 流式 Agent Turn |
| POST | `/stop` | 终止指定或全部活动 Turn |
| POST | `/command` | 执行 Slash Command |

同一个 Session 同时只允许一个活动 Turn。注册失败时返回“Session 正忙”，不会并发修改同一 Session。

### Session 与附件

| 方法 | 路径 |
| --- | --- |
| GET / POST | `/sessions` |
| PATCH / DELETE | `/sessions/{session_id}` |
| GET | `/sessions/{session_id}/messages` |
| GET / POST | `/sessions/{session_id}/attachments` |
| GET | `/sessions/{session_id}/attachments/{attachment_id}` |
| GET | `/sessions/{session_id}/local-image` |

附件端点限制：

- 每条消息最多 4 张图片
- 单张最多 20 MB
- 总请求受 50 MB 附件上限保护
- 保存名随机化并限制后缀
- 元数据按 Session 保存
- 图片读取必须属于当前 Session

### Workspace、回退与审批

| 方法 | 路径 |
| --- | --- |
| POST | `/workspace/pick` |
| GET / POST / DELETE | `/workspace` |
| GET | `/sessions/{session_id}/rollback` |
| POST | `/sessions/{session_id}/rollback/preview` |
| POST | `/sessions/{session_id}/rollback` |
| POST | `/sessions/{session_id}/rollback/undo` |
| GET | `/approvals` |
| POST | `/approvals/{id}/approve` |
| POST | `/approvals/{id}/reject` |

目录选择器在线程中调用本机 Tk 对话框，避免阻塞异步事件循环。

### Memory、Reflection、Cron 与 Skill

```text
GET/POST/PATCH/DELETE  /memories...
GET/PUT/POST           /reflect/config, /reflect/run
GET/POST/DELETE        /cron/jobs...
POST                   /cron/jobs/{id}/enable|disable
GET                    /cron/status
GET                    /skills
GET/DELETE             /skills/{name}
POST                   /skills/upload
```

Skill 上传使用 ZIP 校验和可选替换。Prompt 管理使用：

```text
GET/PUT /admin/system-prompt
GET/PUT /admin/soul
```

更新 Prompt 后刷新 Context Builder 缓存。

### 设置

```text
GET/PUT /settings/llm
GET/PUT /settings/agent
GET/PUT /settings/ui/avatar
GET     /settings/channel
PUT     /settings/channel/qq
POST    /settings/channel/qq/onboard/start
GET     /settings/channel/qq/onboard/{task_id}
GET     /qq/status
```

LLM 与 Agent 设置更新后，Gateway 原地重建可变 Client Holder，不要求为了每次配置修改重启 UI。

### 桌宠与下载

```text
GET/PUT              /pet/settings
GET/POST             /pet/pets
GET/DELETE           /pet/pets/{pet_id}
GET                  /pet/pets/{pet_id}/spritesheet
POST                 /pet/open, /pet/close
GET                  /pet/state
POST                 /pet/runtime/position
POST                 /pet/runtime/closed
GET                  /downloads
GET                  /downloads/{download_id}
```

桌宠进程使用 `X-SJTUClaw-Internal: desktop-pet` 访问少量本地专用路径；只有回环客户端和精确路径组合可获得该豁免。

## SSE

`POST /chat/stream` 用后台线程执行同步 Agent Turn，用线程安全队列把事件送回异步生成器。

主要事件：

```text
_session_info
thinking
tool_call_start
tool_call_end
final
error
```

`_session_info` 携带：

- Session ID
- AUTO
- Sandbox 实际状态
- UNLIMITED
- Pi Mode
- Agent Backend

前端据此更新 Composer 顶部的运行模式状态，不需要等待下一次 Session 列表轮询。

## Web UI

技术栈：

```text
React 18
TypeScript
Vite
Tailwind CSS
Radix UI
react-markdown + GFM + KaTeX
react-syntax-highlighter
Vitest + Testing Library
```

### 组件层

```text
App.tsx
├── Sidebar
├── ThreadShell
│   ├── ThreadViewport
│   └── ThreadComposer
├── SettingsView
│   ├── AgentSettingsSection
│   └── PetSettingsSection
└── PetSelectionContext
```

### 状态与 API

- `useSessions` 管理列表、当前 Session 与刷新。
- `lib/api.ts` 封装 REST、SSE 和附件上传。
- `lib/commands.ts` 定义前端可发现的 Slash Command。
- `lib/commandState.ts` 从命令结果同步模式状态。
- `useTheme` 管理界面主题。

前端不保存明文 API Key；设置端点只返回掩码和“是否已配置”。

### 消息呈现

Thread Viewport 支持：

- Markdown、表格、任务列表
- 数学公式
- 代码高亮
- Tool Call Card
- 图片附件
- `/downloads/<id>` 图片预览和下载按钮
- 回退锚点和分支恢复

### 构建

`webui/vite.config.ts` 把生产结果写到根目录 `web/`。Gateway 在所有 API Route 注册后，将 `/` 挂载为 SPA 静态目录，避免静态路由抢占 API。

## Desktop

`claw/desktop.py`：

1. 选择本地端口。
2. 启动 Gateway。
3. 等待本地 HTTP 可用。
4. 通过 pywebview 打开窗口。
5. 退出时关闭服务。

Desktop 没有独立业务 API；它是 Gateway + Web UI 的 Windows 容器。

## TUI

`claw/tui/` 使用 Textual 8.2.8 构建键盘优先终端驾驶舱。`LocalRuntime` 不发起 HTTP 请求，而是在进程内复用 Gateway 的 Session、Streaming Turn、Slash Command、Approval 和 Cron 处理逻辑。

TUI 已具备完整的 Session / Cron 看板、可搜索命令面板、流式工具状态、内联审批、逐 Session 草稿与输入历史、响应式布局和安全退出清理。组件、事件与状态同步算法见 [[products/terminal-ui]]。

## CLI

`claw/cli/main.py` 提供：

```text
setup
chat
tui
gateway
desktop
```

`setup` 分组配置 LLM、偏好、Gateway、高级参数和 QQ。REPL 拦截 Slash Command，普通文本进入 Agent Runtime。

Slash Command 由 `claw/cli/commands.py` 集中定义，Web 与 TUI 复用同一命令语义。

## QQ

`QQChannel` 实现官方 QQ Bot API v2 WebSocket：

- OAuth Token 和 Gateway 连接
- 私聊、群聊与 Direct Message
- 发送文本、Markdown 和媒体
- Allow List
- ACK 消息
- 重连与退避
- Inline Approval Keyboard

外部 `chat_id` 映射到内部 Session ID。来源 Chat 类型和媒体队列写入 Session / RequestContext，使 Cron 回复可以回到原渠道。

扫码 Onboarding 独立封装创建绑定任务、二维码显示和轮询。

## 桌宠

桌宠由四部分组成：

| 模块 | 责任 |
| --- | --- |
| `PetCatalog` | 内置 / 用户宠物、ZIP 校验和设置 |
| `PetReplyStore` | 每个宠物的点击台词 |
| `PetStateBroker` | 把 Agent 事件变成状态、动画和气泡 |
| `PetProcessManager` | 启停独立 GUI 进程 |

自定义包校验包括路径、文件数、加密、压缩算法、压缩比、清单、真实图片格式、透明通道、固定图集尺寸、必用帧和未用格透明性。

导入后尝试生成 12 条角色台词；只要包本身有效，即使 LLM 失败也使用通用备用台词完成安装。

## 相关页面

- [[concepts/agent-runtime]]
- [[concepts/external-backends]]
- [[patterns/security-boundaries]]
- [[products/terminal-ui]]
- [[products/windows-distribution]]

## 源码依据

- `claw/gateway/__main__.py`
- `claw/gateway/server.py`
- `claw/gateway/middleware.py`
- `claw/gateway/uploads.py`
- `claw/desktop.py`
- `claw/cli/main.py`
- `claw/cli/repl.py`
- `claw/cli/commands.py`
- `claw/tui/`
- `claw/channels/qq.py`
- `claw/channels/qq_onboard.py`
- `claw/pet/`
- `webui/src/`
