# TUI 使用指南

SJTUClaw TUI 是终端UI界面。它与 Web UI、CLI 共用 Session、Agent 后端、Workspace、审批、Cron 和长期记忆。

## 启动

完成项目安装与模型配置后运行：

```powershell
sjtuclaw tui
```

TUI 在当前进程内启动共享运行时，不需要另行启动 Gateway。退出时会清理 Cron、Reflection 和已运行的 Sandbox。

## 界面

- 顶部：任务状态、Session 标题、Agent 后端和安全模式。
- 中间：对话记录、工具调用与实时运行状态。
- 输入区：多行编辑、命令提示、逐 Session 历史和草稿。
- 右侧：模型、Workspace、后端与近期 Cron；窄终端会自动隐藏。
- 审批区：需要确认的工具操作及其参数。

## 快捷键

| 按键 | 功能 |
| --- | --- |
| `Enter` | 发送消息或命令 |
| `Ctrl+N` | 插入换行 |
| `↑` / `↓` | 在首行或末行浏览已发送消息 |
| `Tab` | 接受当前命令补全 |
| `Esc` | 关闭命令提示并返回输入框 |
| `Ctrl+P` | 打开可搜索的命令面板 |
| `Ctrl+S` | 打开 Session Board |
| `Ctrl+J` | 打开 Cron Board |
| `Ctrl+C` | 请求停止当前 Agent 回合 |
| `Ctrl+R` | 刷新界面状态 |
| `Ctrl+Q` | 退出 TUI |

`Ctrl+M` 会被忽略，避免部分终端将它解释成 Enter。输入 `/` 后可用 `↑` / `↓` 选择命令，使用 `Tab` 或 `Enter` 补全。

## Session Board

按 `Ctrl+S` 打开。看板显示标题、最近消息、消息数和更新时间。

- `Enter`：切换到选中 Session
- `/`：搜索标题、最近消息或 Session ID
- `N`：新建
- `E`：重命名
- `X`：删除
- `J` / `K`：向下或向上选择
- `R`：刷新

删除前会二次确认；删除当前 Session 后会自动切换到仍然存在的 Session。

## Cron Board

按 `Ctrl+J` 打开。看板显示启用状态、计划、下次运行、上次状态和 Job ID。

- `Enter`：立即运行
- `Space`：启用或禁用
- `X`：删除用户作业
- `R`：刷新

系统 Cron 作业不能从看板删除，用户作业删除前会二次确认。

## 对话、命令与审批

普通文本进入当前 Session 的 Agent 后端。TUI 会实时显示思考阶段、工具名称与参数、工具结果、耗时、错误和最终回复。

输入 `/` 可访问完整命令族，包括 Session、Memory、Workspace、Sandbox、Rollback、Skill、Reflection、Cron、宠物、安全模式以及 Pi / Claude Code 后端切换。`Ctrl+P` 可搜索并把命令插入输入框。

任务运行期间会保留新草稿，避免误发；仍可使用 `/stop`、`/approvals`、`/approve` 和 `/reject`。需要审批的操作会显示在输入区上方，可直接批准或拒绝。

## 窄终端

界面会根据宽高自动调整：

- 较窄时隐藏右侧运行信息栏。
- 更窄时隐藏后端和安全模式标签及发送提示。
- 高度不足时压缩输入区和审批区。

建议使用支持现代终端控制序列和 Unicode 的终端，例如 Windows Terminal。

## 相关文档

- [配置说明](configuration.md)
- [测试与开发](testing.md)
- [Code Wiki：Terminal UI](code-wiki/products/terminal-ui.md)
