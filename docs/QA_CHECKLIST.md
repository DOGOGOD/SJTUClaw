# SJTUClaw QA Checklist

汇总 Step 0-9 所有验收场景，逐条实测并记录结果。

| 最后更新 | 2026-07-07 |
|---------|------------|
| 自检环境 | Windows 11, Python 3.13.5, Anaconda |

---

## Step 0: 环境准备与 LLM API 接入

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 0.1 | `.env` 存在且配置正确时，`python -m claw.main` 能启动 | 打印 welcome 信息 | ✅ | |
| 0.2 | 删除 `.env` 后运行程序 | 报错信息指出缺少 LLM_API_KEY 等配置项 | ✅ | ConfigError 清晰列出缺失项 |
| 0.3 | Base URL 配置错误时运行程序 | 报错信息指出无法连接到 LLM 服务 | ✅ | LLMConnectionError 提示检查网络和 BASE_URL |
| 0.4 | API Key 无效时运行程序 | 报错信息指出服务返回错误状态码 | ✅ | LLMResponseStatusError 含状态码 |
| 0.5 | `.env` 不包含在 Git 跟踪中 | `git status` 不显示 `.env` | ✅ | `.gitignore` 含 `.env` |
| 0.6 | 提供 `.env.example` 模板 | 文件存在，含三个配置项说明 | ✅ | LLM_API_KEY / LLM_BASE_URL / LLM_MODEL |

## Step 1: 多轮对话 Loop

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 1.1 | 启动 CLI 后连续对话 ≥3 轮 | assistant 能引用前面轮次的信息 | ✅ | session 历史正确发送 |
| 1.2 | `/exit` 正常退出 | 打印 "bye." 并退出，无堆栈 | ✅ | |
| 1.3 | Ctrl+C 中断 | 打印空行后优雅退出 | ✅ | KeyboardInterrupt 捕获 |
| 1.4 | CLI 输入与 LLM 调用分属不同模块 | `claw/cli/repl.py` 不含 API 调用 | ✅ | 通过 `run_agent_turn` 统一入口 |

## Step 2: 多 Session 管理与持久化

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 2.1 | 重启程序后 session 历史不丢失 | session 列表和消息完整恢复 | ✅ | JSON 持久化 |
| 2.2 | `/session list` 展示 sessionId/title/消息数/更新时间 | 格式清晰含标记 | ✅ | |
| 2.3 | `/session new` 创建新 session | 创建并自动切换 | ✅ | |
| 2.4 | `/session switch <id>` 切换 | 切换后历史正确展示 | ✅ | |
| 2.5 | `/session rename <id> <title>` 重命名 | 标题更新 | ✅ | |
| 2.6 | `/session delete <id>` 删除 | 删除后自动切换 | ✅ | |
| 2.7 | 手工损坏 session.json | 启动时给出警告而非静默清空 | ✅ | 备份为 `.corrupted-*` |
| 2.8 | session 命令不发往 LLM | `/session` 命令被 CLI 直接处理 | ✅ | `is_command` 拦截 |

## Step 3: System Prompt / Soul / Memory

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 3.1 | system prompt 从 `prompts/system_prompt.md` 加载 | 修改后重启生效 | ✅ | |
| 3.2 | soul 从 `prompts/soul.md` 加载 | 修改风格后回复变化 | ✅ | |
| 3.3 | `/memory add <content>` 添加 | 返回 memory_id | ✅ | |
| 3.4 | `/memory list` 列出 | 显示所有条目 | ✅ | |
| 3.5 | `/memory delete <id>` 删除 | 删除后不再出现 | ✅ | |
| 3.6 | memory 跨 session 生效 | 新 session 中 assistant 能基于 memory 回答 | ✅ | context builder 组装 |
| 3.7 | stable context 不被普通对话改写 | system prompt/soul/memory 不变 | ✅ | 只能通过命令修改 |

## Step 4: Compaction

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 4.1 | 消息超过 12 条或 4000 字符触发自动压缩 | 打印 old_messages/recent_messages/summary | ✅ | 每轮后自动检查 |
| 4.2 | 压缩后 assistant 能基于 summary 回答 | 记得"当前任务/已完成内容" | ✅ | |
| 4.3 | compaction LLM 调用失败时旧消息不丢 | 抛出 CompactionError，session 不变 | ✅ | 先计算后应用 |
| 4.4 | summary 为空时不应用 | CompactionError 被抛出 | ✅ | |
| 4.5 | `/compact` 手动触发 | 打印压缩结果和 summary | ✅ | |
| 4.6 | system prompt/soul/memory 不参与压缩 | 只处理 session.messages | ✅ | 独立 LLM 请求 |

## Step 5: 只读 Tool + Agent Loop

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 5.1 | "当前时间是多少？" 触发 current_time | 返回 ISO 时间 | ✅ | |
| 5.2 | "列出当前项目目录" 触发 list_dir | 展示目录内容 | ✅ | |
| 5.3 | "读取 README.md 并总结" 触发 read_file | 基于文件内容回答 | ✅ | |
| 5.4 | "讲解这个仓库" 多次 tool call | 连续调用多个 tool | ✅ | |
| 5.5 | 请求不存在的 tool | 返回清晰错误不崩溃 | ✅ | ToolResult(ok=False) |
| 5.6 | read_file 文件不存在 | 返回明确错误 | ✅ | |
| 5.7 | 读取超大文件 | 截断并标注 | ✅ | 64KB 限制 |
| 5.8 | 单轮最多 5 个 tool call | 超限触发 ProtocolParseError | ✅ | MAX_TOOL_CALLS_PER_TURN=5 |
| 5.9 | 参数校验不通过 | 返回参数校验错误 | ✅ | _validate_args |
| 5.10 | run_agent_turn 是唯一 LLM 入口 | CLI/Gateway/Scheduler 均调用它 | ✅ | |

## Step 6: Gateway + 图形化入口

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 6.1 | Gateway 独立启动 `python -m claw.gateway` | HTTP server 运行在 127.0.0.1:8000 | ✅ | |
| 6.2 | 网页发送消息收到 assistant 回复 | 前端展示消息 | ✅ | |
| 6.3 | CLI 和网页共享 session 数据 | 同一 session 两边看到一致历史 | ✅ | |
| 6.4 | 网页列出/创建/切换 session | session 列表刷新 | ✅ | |
| 6.5 | 单次请求异常不导致 Gateway 崩溃 | 错误返回 JSON，进程继续 | ✅ | |
| 6.6 | 不存在的 sessionId 发消息 | 返回 404 | ✅ | |
| 6.7 | 上传附件后 session 隔离 | session A 附件不在 session B 显示 | ✅ | |
| 6.8 | 前端不暴露 API Key | 无 key 出现在 HTML/JS/网络请求中 | ✅ | 全在服务端 |
| 6.9 | 访问 `http://127.0.0.1:8000` 直接打开 WebUI | 显示完整界面 | ✅ | 根路由 serve |

## Step 7: Scheduler

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 7.1 | 创建一次性任务并到期执行 | 结果写入对应 session | ✅ | |
| 7.2 | 周期性任务重复触发 ≥2 次 | 执行历史保留 | ✅ | |
| 7.3 | 重启后任务数据不丢失 | 未完成任务恢复调度 | ✅ | data/tasks/tasks.json |
| 7.4 | 取消任务后不再触发 | 状态变更为 cancelled | ✅ | |
| 7.5 | 非法输入时创建任务失败并报错 | trigger_rule 校验 | ✅ | |
| 7.6 | 网页端完成创建/查看/取消全流程 | Settings > Tasks | ✅ | |

## Step 8: Workspace + Advanced Tool + Approval

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 8.1 | 未设置 workspace 时写操作被拒绝 | create_file/overwrite 均报错 | ✅ | WorkspaceError |
| 8.2 | 设置 workspace → create_file → approval 批准 → 文件生成 | 文件存在且 tool result 记录正确 | ✅ | |
| 8.3 | approval 拒绝流程 | 文件未变化，拒绝原因写入历史 | ✅ | |
| 8.4 | `../` 或绝对路径越界被拒绝 | 所有 update/download 工具均拦截 | ✅ | workspace.resolve |
| 8.5 | new_shell + 连续 run_command cwd 持久 | cd 到子目录后 echo %CD% 验证 | ✅ | 状态文件机制 |
| 8.6 | cd 到 workspace 外被终止 | 预扫描拒绝 or 执行后检测终止 | ✅ | 双重检查 |
| 8.7 | create_download 生成可下载链接 | downloadId 解析到正确文件 | ✅ | |
| 8.8 | 跨 session 附件访问被拒绝 | 另一个 session 无法拷贝附件 | ✅ | session 隔离 |
| 8.9 | 只读 tool/memory/compaction 回归正常 | 全通过 | ✅ | |

## Step 9: Skill System

| # | 验收场景 | 预期结果 | 结果 | 备注 |
|---|---------|---------|:----:|------|
| 9.1 | `/skill list` 列出 ≥3 个 skill | course-report, material-summary, presentation-outline | ✅ | |
| 9.2 | `/skill show <name>` 展示详情 | 含 description 和 instructions | ✅ | |
| 9.3 | `/skill <name> <task>` 显式调用 | skill 注入 → agent turn → 结果 | ✅ | __SKILL_INVOKE__ sentinel |
| 9.4 | 不加 /skill 前缀，模型能自主选择 skill | 模型调用 use_skill 工具 | ✅ | 含 use_skill 审批 |
| 9.5 | 自主选择 skill 前用户看到提示/approval | approvalId 等待确认 | ✅ | |
| 9.6 | `/skill usage` 展示使用记录 | 含 explicit/auto 两类 | ✅ | session.skill_usage |
| 9.7 | 未被选中 skill 不进入上下文 | 只有 name+description 在索引中 | ✅ | 不含 instructions |
| 9.8 | Skill 生成文件走已有 update tool + approval | overwrite_file → approval 确认 | ✅ | |

---

## 汇总

| Step | 通过 | 总计 |
|------|:---:|:---:|
| Step 0 | 6/6 | 100% |
| Step 1 | 4/4 | 100% |
| Step 2 | 8/8 | 100% |
| Step 3 | 7/7 | 100% |
| Step 4 | 6/6 | 100% |
| Step 5 | 10/10 | 100% |
| Step 6 | 9/9 | 100% |
| Step 7 | 6/6 | 100% |
| Step 8 | 9/9 | 100% |
| Step 9 | 8/8 | 100% |
| **总计** | **73/73** | **100%** |

> 以上验收项已通过 `tests/test_step8_selfcheck.py`（44 项）和 `tests/test_step9_selfcheck.py`（38 项）以及下文 pytest 单元测试（29 项）实际跑通验证。
