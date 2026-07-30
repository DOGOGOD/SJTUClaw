# SJTUClaw Code Wiki

> 面向代码阅读者的实现说明。内容以当前工作区代码为准，覆盖 Python Runtime、React Web UI、外部 Agent、Sandbox、持久化和 Windows 分发。

## 如何阅读

Code Wiki 按“一个页面解释一个核心知识实体”组织。建议按目标选择路径：

| 目标 | 推荐阅读顺序 |
| --- | --- |
| 理解一次请求如何完成 | Agent Runtime → Tool System → Session and Context |
| 修改安全、审批或文件边界 | Tool System → Security Boundaries → Persistence Layout |
| 修改 Web UI 或 Gateway | Gateway and Clients → Agent Runtime → Persistence Layout |
| 修改 TUI | Terminal UI → Gateway and Clients → Agent Runtime |
| 修改记忆、Skill 或定时任务 | Memory Skill Scheduler → Session and Context |
| 接入新的 Agent 后端 | External Backends → Agent Runtime → Security Boundaries |
| 构建 Windows 版本 | Windows Distribution → Persistence Layout → Sandbox 架构 |

## 页面目录

### 核心概念

- [Agent Runtime](code-wiki/concepts/agent-runtime.md)
  入口汇合、运行时初始化、一次 Agent Turn 的完整状态机、事件和失败收尾。

- [Session and Context](code-wiki/concepts/session-context.md)
  `Message` / `Session` 模型、JSONL、Context Builder、Token 预算、压缩和分叉。

- [Tool System](code-wiki/concepts/tool-system.md)
  Tool 数据结构、18 个内置工具、参数校验、并发、结果落盘和宿主工具桥接。

- [Memory, Skill and Scheduler](code-wiki/concepts/memory-skill-scheduler.md)
  Markdown 记忆、Reflection、Skill Registry、Cron、Heartbeat 和任务投递。

- [Gateway and Clients](code-wiki/concepts/gateway-clients.md)
  FastAPI 生命周期、REST / SSE、Web UI、TUI、CLI、QQ 和桌宠。

- [External Backends](code-wiki/concepts/external-backends.md)
  原生 Agent、Pi、Claude Code 的 Session 路由、会话恢复、事件和审批差异。

### 工程模式

- [Security Boundaries](code-wiki/patterns/security-boundaries.md)
  Approval、Workspace、AUTO、UNLIMITED、Sandbox、SSRF、Gateway 鉴权和 fail-closed。

- [Persistence Layout](code-wiki/patterns/persistence-layout.md)
  路径切换、原子写入、文件锁、数据目录、回退对象和运行设置加密。

### 产品与分发

- [Terminal UI](code-wiki/products/terminal-ui.md)
  Textual 组件树、LocalRuntime、流式事件、Session / Cron 看板、审批和响应式交互。

- [Windows Distribution](code-wiki/products/windows-distribution.md)
  pywebview 桌面壳、静态 Web 资源、PyInstaller、Inno Setup 和安装版路径。

## 代码地图

| 路径 | 责任 | 主要入口 |
| --- | --- | --- |
| `claw/agent/` | 原生 Agent Loop、事件、预算、指标、健康监控 | `run_agent_turn()` |
| `claw/context/` | Prompt 组装、Token 预算、压缩与结构治理 | `ContextBuilder.build()` |
| `claw/llm/` | OpenAI Compatible 调用与协议解析 | `LLMClient.chat_with_tools()` |
| `claw/session/` | Message / Session 模型、JSONL、标题 | `SessionStore` |
| `claw/tools/` | 工具定义、注册、边界和具体工具 | `register_all_tools()` |
| `claw/approval/` | 待审批请求的生命周期 | `ApprovalManager` |
| `claw/workspace/` | Session 绑定、路径解析、回退 | `WorkspaceManager`、`WorkspaceRollbackManager` |
| `claw/sandbox/` | microsandbox 配置、生命周期与双路由 | `SandboxManager` |
| `claw/memory/` | 长期记忆和每日反思 | `MemoryStore`、`ReflectionManager` |
| `claw/skills/` | Skill 扫描、加载、统计和生命周期 | `SkillRegistry` |
| `claw/scheduler/` | Cron Store、定时器、调度与 Heartbeat | `CronService` |
| `claw/gateway/` | FastAPI 应用、SSE、上传下载与设置 | `claw.gateway.server:app` |
| `claw/cli/` | 配置向导、REPL 和 Slash Command | `claw.cli.main:main` |
| `claw/tui/` | Textual 终端驾驶舱与进程内运行时适配 | `SJTUClawTUI`、`LocalRuntime` |
| `claw/pi/` | Pi JSONL RPC 和 Session 路由 | `PiAgentClient` |
| `claw/claude/` | Claude Code stream-json、Hook 和 MCP | `ClaudeCodeAgentClient` |
| `claw/channels/` | 外部消息渠道 | `QQChannel` |
| `claw/pet/` | 宠物目录、进程、状态和台词 | `PetCatalog`、`PetProcessManager` |
| `webui/src/` | React 客户端、API、线程和设置 | `App.tsx` |
| `packaging/` | Sandbox 镜像和 Windows 分发 | `build.ps1` |

## 进程模型

### Gateway / Desktop

Gateway 进程持有共享的 Store、Registry、调度器、Reflection、QQ 和桌宠进程管理器。Agent Turn 在线程中执行，SSE 端点通过队列把事件桥接回异步响应。

Desktop 不是另一套 Runtime。`claw/desktop.py` 启动本地 Gateway，再由 pywebview 打开其 Web UI。

### CLI / TUI

CLI 直接组装与 Gateway 相同的核心对象。TUI 的 `LocalRuntime` 在进程内调用 Gateway 的规范处理函数，不启动 HTTP 服务；完整实现见 [[products/terminal-ui]]。

### 外部子进程

- Pi：每回合启动 JSONL RPC 子进程，原生 transcript 持久化并在后续回合恢复。
- Claude Code：每轮启动或恢复 `claude` stream-json 进程，并管理 Windows Job / 进程树。
- 桌宠：独立 GUI 进程，通过受限的本地 Gateway 路径同步状态。
- microsandbox：每个有效 Session 一个 microVM 运行实例，持久 Volume 与进程实例分离。

## 一次用户请求的最短调用链

```text
入口接收消息
→ 确定或创建 Session
→ 保存用户消息与 Workspace 检查点
→ 选择 Session Agent 后端
→ 构建上下文或交给外部 Agent
→ 模型返回最终文本或工具调用
→ 校验、审批并执行工具
→ 工具结果写回 Session
→ 继续迭代，直到最终回复、停止、失败或预算耗尽
→ 保存 Session、发布事件、触发自动标题/压缩/桌宠状态
```

## 稳定设计约束

1. **Session 是隔离单位。** 后端选择、Workspace、AUTO、显式 Sandbox、附件和回退分支均按 Session 处理。
2. **原始会话优先保留。** 压缩推进上下文投影，不删除原始消息。
3. **高风险操作默认需要批准。** 没有可用审批通道时拒绝，而不是假定允许。
4. **显式安全要求 fail-closed。** 显式 Sandbox 或 `required` 不得回退宿主。
5. **路径解析在 I/O 前完成。** Workspace、附件、宠物包和下载都先规范化并检查边界。
6. **持久化写入尽量原子化。** 关键 JSON / JSONL 使用文件锁、唯一临时文件和替换。
7. **入口不复制 Agent 逻辑。** Web、CLI、QQ、Cron 和 TUI 最终共享同一 Session 与 Agent Runtime。
8. **外部 Agent 保留原生能力。** SJTUClaw 负责路由、呈现和边界桥接，不重写 Pi / Claude Code 的内部循环。

## 维护规则

- 修改核心调用链时，同步更新对应专题页和本索引。
- 新增跨模块实体时才新增页面；局部实现细节留在现有页面。
- 页面底部列出“相关页面”和“源码依据”。
- 更新记录追加到 [Code Wiki Log](code-wiki/log.md)。

最后同步：2026-07-30。
