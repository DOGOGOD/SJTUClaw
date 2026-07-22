# SJTUClaw 任务型验收方案

## 一、验收原则

本方案依据 `SJTUClaw.md` 的 Step 0—Step 9 设计。动态任务使用独立的 Session、数据目录与 Workspace；所有材料均为本次验收专门生成的合成内容。评分时区分三类证据：

1. **任务结果**：Claw 的最终回复或生成文件是否满足要求。
2. **过程证据**：Session 历史中是否存在正确的 Tool Call、Tool Result、Approval、Skill 使用记录和 Scheduler 运行记录。
3. **安全证据**：跨 Session 数据是否隔离；写入是否在批准前不发生；拒绝后是否无副作用；文件是否被限制在 Workspace 内。

仅有 API、按钮或代码文件，不等同于任务通过；必须形成真实运行闭环。

## 二、任务集

| ID | 对应评分项 | 交付给 Claw 的核心任务 | 难点与验收点 |
| --- | --- | --- | --- |
| T00 | Step 0 | 只返回精确哨兵字符串 `CLAW-BASE-OK` | 验证真实模型配置、请求和回复解析；不得返回配置错误或伪造成功。 |
| T01 | Step 1 | 第一轮记住 `ORBIT-731` 与“后三位乘 2”规则，第二轮只给计算结果 | 必须依赖历史得到 `1462`，验证 user/assistant 历史均进入模型上下文。 |
| T02 | Step 2 | 在全新 Session 询问是否见过 ORBIT 代号，并执行 Session 列表、重命名与重新加载 | 新 Session 必须回答未知且不得泄漏 `ORBIT-731`；Session 数据应可由新的 Store 实例恢复；内部命令不得发给模型。 |
| T03 | Step 3 | 添加长期偏好“课程报告结论先行”和标记“琥珀-417”，在另一 Session 询问 | 同时验证 Memory 增查与跨 Session 稳定上下文召回。 |
| T04 | Step 4 | 对包含项目代号、硬约束、偏好和风险的长历史执行 `/compact`，随后询问项目代号 | Summary 必须非空并持久化；原始消息不能因失败丢失；压缩后应召回 `NEBULA-2049`。 |
| T05 | Step 5 | 列目录并读取课程笔记与实验 CSV，推荐 variant，解释三节点网络分区 | 必须真实调用目录与读文件工具；结论应包含 B 的 82 ms、可靠性对比及多数派提交逻辑。 |
| T06 | Step 6 | 在一个 Session 上传附件，再从另一个 Session 查询附件 | Gateway 应返回清晰结果；附件 metadata 必须隔离，另一 Session 数量为 0。 |
| T07 | Step 7 | 创建 7 秒后执行的一次性任务，要求只回复 `CRON-EXECUTED-2026` | 必须保存任务计划并由 Scheduler 调用统一 Agent Loop；结果需回写所属 Session，且运行历史/状态可查。 |
| T08 | Step 8 | 在绑定 Workspace 中生成报告文件，批准写入；随后请求创建第二文件并拒绝 | 批准前文件不得出现；批准后才写入；拒绝后目标文件必须不存在，拒绝结果应进入 Session 历史。 |
| T09 | Step 9 | 明确要求发现并加载 `course-report` 及 checklist，读取材料，生成 900—1200 字报告并保存 | 必须出现 `skills_list`、`skill_view`、参考资源读取和 `overwrite_file`；产物需含摘要、三节正文、分区案例、variant 对比、结论、参考资料。 |
| T09M | Step 9 | 列出 Skill 并查询当前 Session 使用记录 | 至少存在三个 Skill，必须包含 `course-report`；入口和记录应可见。 |
| T09E | Step 9 | 通过 Gateway `/command` 显式执行 `/skill course-report <task>` | Gateway 必须消费内部调用信号并启动 Agent，不得把 `__SKILL_INVOKE__` 暴露给图形客户端。 |

## 三、动态评分方法

Step 0—Step 5 每项满分 10 分，Step 6—Step 9 每项满分 5 分。

- **满分**：核心任务结果、过程证据和安全要求全部满足。
- **60%—80%**：主流程成功，但缺少一类非关键证据，或输出质量存在小问题。
- **30%—50%**：入口存在但闭环不完整，例如工具执行后未继续回答、定时任务未回写、Skill 仅列出但未加载。
- **0%—20%**：任务失败、数据串 Session、审批前产生副作用、伪造工具结果或出现不可恢复的数据丢失。

代码质量与整体完成度 10 分，依据模块复用、安全边界、异常处理、测试覆盖和构建结果评分。中期报告 10 分，依据内容完整性、与当前实现一致性、架构说明、问题分析和后续计划评分。

## 四、隔离与产物

- 隔离运行数据：`.claw-eval-data/`
- 隔离 Workspace：`claw_evaluation_workspace/`
- 自动执行器：`evaluation/run_claw_acceptance.py`
- 机器可读结果：`evaluation/claw_acceptance_results.json`
- 最终人工评分报告：`evaluation/SJTUClaw验收报告.md`

默认执行器使用本地确定性模型替身驱动真实 Runtime，不会把材料发送到外部服务。若改为当前 `.env` 配置的真实模型进行批量验收，则会发送合成测试材料、内置 system prompt 与内置 Skill 文档，运行前需要用户明确知情授权。
