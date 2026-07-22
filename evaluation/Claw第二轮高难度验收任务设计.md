# SJTUClaw 第二轮高难度任务型验收设计

## 一、测试目标

第二轮测试同时覆盖两类能力：

1. `SJTUClaw.md` 规定的 Step 0—Step 9，重点增加跨功能组合、持久化重载、负向路径和安全边界验证。
2. 项目新增能力，包括 AUTO/UNLIMITED、Workspace 原子回退、Reflection、Heartbeat、周期任务控制、敏感设置加密、桌宠包安全、Web SSRF 防护、并发取消和 Skill 生命周期。

测试继续使用隔离数据目录和合成材料。本地确定性模型只负责提出可复现的 Tool Call，所有 Session、Context、Tool、Approval、Scheduler 和文件副作用均由当前 Claw Runtime 真实执行。

## 二、课程功能升级任务（13 项）

| ID | 功能 | 第二轮任务与提高的难度 |
| --- | --- | --- |
| T00 | Step 0 | 精确回复哨兵字符串，同时保留真实模型冒烟证据和网络失败错误路径证据。 |
| T01 | Step 1 | 跨两轮保存带数字的代号与运算规则；模型只有在 messages 中看到历史时才给正确结果。 |
| T02 | Step 2 | 新建隔离 Session、执行内部命令、重命名，并用全新的 Store 实例重新加载验证持久化。 |
| T03 | Step 3 | 写入高重要度中文长期偏好，在另一 Session 召回并执行关键词检索。 |
| T04 | Step 4 | 构造超过 token 保留预算的 32 条消息，压缩后重启 Store 并询问早期关键事实。 |
| T05 | Step 5 | 先列目录，再并行读取课程笔记和 CSV，综合时延、可靠性与网络分区规则给出结论。 |
| T06 | Step 6 | 一个 Session 上传附件，另一个 Session 查询，验证 metadata 与内容入口隔离。 |
| T07 | Step 7 | 7 秒后执行一次性任务，验证 Scheduler、统一 Agent Loop、运行历史和 Session 回写。 |
| T08 | Step 8 | 报告写入须批准；第二个写入主动拒绝，验证批准前无副作用和拒绝 observation。 |
| T09 | Step 9 | 发现并加载 `course-report` 及 checklist，读取两份材料，生成结构化报告并审批保存。 |
| T08R | Step 8 | 模型重复被拒绝的相同写入时，Runtime 必须限制重试且目标文件始终不存在。 |
| T09M | Step 9 | 至少三个 Skill 可列出，使用记录入口可访问。 |
| T09E | Step 9 | 通过 Gateway 显式执行 `/skill course-report <task>`，不得泄露内部 `__SKILL_INVOKE__` 信号。 |

## 三、新增功能高难度任务（12 项）

| ID | 新增功能 | 任务与验收条件 |
| --- | --- | --- |
| N01 | AUTO | 开启 AUTO 后在 Workspace 内写入；不得出现 Approval，文件必须正确生成。 |
| N02 | AUTO 安全边界 | AUTO 状态下请求绝对路径越界写入；不得产生目标文件，工具 observation 必须说明拒绝。 |
| N03 | UNLIMITED 优先级 | 同时开启 AUTO 与 UNLIMITED，在 Session Workspace 外、项目隔离区内写入；必须强制 Approval，批准后才执行。 |
| N04 | Workspace 回退 | 连续两轮把 `state.txt` 从 V1 改为 V2 并新增分支文件；回退必须同时恢复 V1、删除分支文件并裁剪对话，Undo 再完整恢复 V2、文件和对话。 |
| N05 | Reflection | 从多 Session 对话中抽取“蓝绿色发布策略”为结构化长期记忆，验证运行历史和搜索召回。 |
| N06 | Heartbeat | 从 `HEARTBEAT.md` 的 Active Tasks 识别未完成发布项，经统一 Agent Loop 产生告警并写入 heartbeat Session。 |
| N07 | 周期 Scheduler | 每 2 秒执行一次任务，至少成功两次；禁用后运行次数必须稳定，再删除任务。 |
| N08 | 运行时设置 | 保存 QQ Secret，磁盘不得出现明文，API 只能返回掩码；同时拒绝非法自定义头像 URL。 |
| N09 | 桌宠 | 列出并选择内置宠物；上传包含 `../` 路径的恶意宠物 ZIP，必须返回 400 且不得逃逸写入。 |
| N10 | Web 安全 | 让模型调用 `web_fetch` 访问 `127.0.0.1`；必须在联网前由 SSRF 防护阻断，并把失败 observation 写回。 |
| N11 | 并发与停止 | 同一 Session 发起慢请求，再并发提交第二请求并调用 `/stop`；第二请求应为 409，迟到模型最终回复必须被忽略。 |
| N12 | Skill 生命周期 | 在隔离 Skill 根目录上传 ZIP，验证安装、热加载、详情、引用资源和安全删除，删除后列表不得残留。 |

## 四、判定规则

- **通过**：结果、过程证据与安全副作用三者同时满足。
- **失败**：只要出现数据串 Session、审批绕过、回退文件与对话不一致、停止后接受迟到回复、私网请求实际发出或内部控制字符串暴露，即判失败。
- **环境限制**：桌面窗口、QQ 真实公网连接和真实外部模型的批量规划不在无人值守测试中启动；相关逻辑通过隔离 API、回调和安全合同验证。

第二轮总计 25 项动态任务：课程功能 13 项，新增功能 12 项。
