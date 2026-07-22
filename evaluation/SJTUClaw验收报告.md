# SJTUClaw 功能验收与评分报告

## 1. 结论

本轮按照 `SJTUClaw.md` 的百分制标准执行了任务型验收。综合得分为 **96/100**。

| 评分项 | 得分 | 结论 |
| --- | ---: | --- |
| Step 0—Step 5 基础功能 | 60/60 | 六项核心闭环均通过任务验证 |
| Step 6—Step 9 高阶功能 | 19/20 | Gateway 显式 Skill 调用存在 1 个确定缺陷 |
| 代码质量与整体完成度 | 9/10 | 后端、前端测试及生产构建通过，架构复用与安全边界较完整 |
| 中期报告 | 8/10 | 内容完整，但测试数字已过期，并把仍有缺陷的 Step 9 写成完全完成 |
| **总分** | **96/100** | 功能完成度高，建议优先修复显式 Skill 调用链 |

机器可读动态结果为 **12 项通过、1 项失败**。失败项不是模型生成质量问题，而是 Gateway 把内部控制字符串直接返回给客户端。

## 2. 证据边界

本次使用两层验证：

1. **真实模型冒烟**：通过当前 `.env` 配置的模型服务执行精确字符串任务，Claw 成功返回 `CLAW-SMOKE-OK`。在网络被沙箱禁止时，Claw 也返回了清晰的连接错误与失败简报，验证了 Step 0 的错误路径。
2. **隔离动态验收**：因未获得批量发送项目 prompt 与 Skill 文档到外部模型服务的明确授权，其余任务由本地确定性模型替身驱动，但完整经过现有 Gateway、Context Builder、Session Store、Agent Loop、Tool Registry、Approval、Scheduler、Workspace、Memory、Compaction 和 Skill Registry。它能验证 Runtime 是否把模型请求正确执行成真实副作用与持久化结果，但不用于评价外部模型自身的自由规划能力。

所有动态数据都在独立数据目录和 `claw_evaluation_workspace/` 中产生；临时 Session/Memory/Cron 数据在取证后已删除，仅保留任务、结果 JSON 和报告产物。

## 3. 分项结果

### Step 0：环境准备与 LLM API 接入 — 10/10

- `.env` 未被 Git 跟踪，且被 `.gitignore` 正确忽略。
- 真实模型调用成功返回精确哨兵字符串。
- 网络受限时，日志给出连接失败、重试和配置检查提示，用户侧得到失败简报而非程序崩溃。
- 仓库内发现的两个 `sk-...` 字符串均位于脱敏单元测试中，不是真实密钥。

### Step 1：多轮对话 Loop — 10/10

- T01 首轮保存 `ORBIT-731` 与计算规则，次轮得到 `1462`。
- 本地模型在回答前检查实际输入 messages；缺少历史时会返回 `MISSING-CONTEXT`，因此通过结果证明当前 Session 历史确实进入模型上下文。
- user 与 assistant 消息均被持久化。

### Step 2：多 Session 管理与持久化 — 10/10

- T02 新 Session 未看到另一 Session 的 `ORBIT-731`，返回 `UNKNOWN`。
- 创建、列表、重命名均成功。
- 使用新的 `SessionStore` 实例重新加载后，两个 Session 仍存在。
- 将 `/session list` 发送到 `/chat` 时由 Runtime 识别为 command，没有进入模型调用。

### Step 3：System Prompt、Memory 与 Soul — 10/10

- 通过 Gateway 添加 `user_preference` 长期记忆。
- 新 Session 的 Context Builder 中出现“结论先行”和“琥珀-417”，模型据此正确回答。
- Memory 关键词查询成功返回记录。
- system prompt 与 soul 均由独立资源加载，相关 API 与自动化测试通过。

### Step 4：Compaction — 10/10

- 构造超过保留预算的 32 条 Session 消息并执行 `/compact`。
- Summary 生成并由新的 `SessionStore` 实例重新加载成功。
- 压缩后询问项目代号仍返回 `NEBULA-2049`。
- 后端测试覆盖压缩失败保留原始消息、摘要边界持久化和回退后的 revision 防护。

### Step 5：只读 Tool 与 Agent Loop — 10/10

- T05 实际执行 `list_dir` 和两次 `read_file`。
- Tool Result 写回后，Agent 继续推理并正确引用：B 为 82 ms、C 的成功率为 0.998、三节点多数派一侧可继续提交。
- 过程记录中存在 Tool Call 与 Tool Result，而非模型伪造环境结果。

### Step 6：Gateway 与图形化入口 — 5/5

- Gateway Session、消息、附件与错误接口工作正常。
- T06 在一个 Session 上传附件后，另一 Session 的附件列表仍为空。
- Web UI 前端 41 项测试全部通过，生产构建成功。
- Gateway SSE、Session 切换、附件和审批相关后端测试通过。

### Step 7：Scheduler — 5/5

- T07 创建 7 秒后触发的一次性任务。
- Scheduler 实际 dispatch 到统一 Agent Loop，得到 `CRON-EXECUTED-2026`。
- 结果写回指定 Session；任务状态与运行记录可查询。

### Step 8：Workspace、Advanced Tool 与 Approval — 5/5

- Session 成功绑定专用 Workspace。
- 报告写入触发 Approval，批准后才产生文件。
- 第二次写入被明确拒绝，`must_not_exist.md` 未创建，拒绝 observation 进入 Session 历史。
- 生成文件位于 Workspace 内，未出现越界副作用。

### Step 9：Skill System — 4/5

通过部分：

- Skill Registry 列出至少三个 Skill，包含 `course-report`。
- T09 完整执行 `skills_list`、`skill_view(course-report)`、读取 `references/checklist.md`、读取两份 Workspace 材料、`overwrite_file` 和 Approval。
- 生成的报告包含摘要、三个以上正文小节、三节点网络分区、variant 数据对比、结论和参考资料。

失败部分：

- T09E 调用 Gateway `/command` 并提交 `/skill course-report <task>` 后，响应直接暴露：

  ```text
  __SKILL_INVOKE__|course-report|只生成一段标题为显式调用验证的课程报告草稿，不写文件。
  ```

- `claw/cli/commands.py` 把显式调用转换为内部 sentinel，CLI REPL 会消费它并启动 Agent；`claw/gateway/server.py` 的 `/command` 路径却直接把 sentinel 作为 `result` 返回。Web/图形入口因此没有形成显式 Skill 调用闭环。

## 4. 代码质量与整体完成度 — 9/10

通过证据：

- 后端：`394 passed, 2 subtests passed`。
- 前端：`9` 个测试文件、`41` 项测试全部通过。
- Vite 生产构建成功，转换 2122 个模块。
- `.env` 未跟踪且被忽略。
- CLI、Gateway、QQ、Scheduler 与 Heartbeat 的调用点都汇入 `run_agent_turn()`，符合统一 Runtime 要求。
- Workspace、UNLIMITED、Approval、附件路径、Skill ZIP 与 SSRF 等安全路径有专门回归测试。

扣分原因：

- 现有测试没有覆盖“Gateway 必须消费 `__SKILL_INVOKE__`”这一入口一致性要求，因此 394 项后端测试全绿仍遗漏了真实用户路径缺陷。
- 首次使用系统临时目录运行 pytest 时受权限影响产生 109 个 setup error；指定 Workspace 内 `--basetemp` 后 394 项全部通过。这属于验收环境问题，不计为代码测试失败，但说明测试文档可补充 Windows 受限环境建议。

## 5. 中期报告 — 8/10

优点：

- 覆盖项目概述、Step 0—9、架构、核心数据结构、特色、未完成项与开发计划。
- 统一 Runtime、Memory、Compaction、Tool、Approval 与桌面打包说明清晰。
- Mermaid 调用链能准确表达关键模块关系。

扣分原因：

- 报告声称“Python 自动化测试 326 项全部通过”，当前实际为 394 项，数字已过期。
- 报告将 Step 9 的显式调用和图形入口写为完成，但动态验收发现 Gateway 显式调用失败。
- 章节编号从 5.4 跳到 5.6，且缺少与当前测试报告、构建产物或关键验收证据的链接。

## 6. 主要缺陷与建议

### P1：Gateway 显式 Skill 调用未执行

建议让 Gateway 与 CLI REPL 共用同一个“解析 command 后决定是否启动 Agent Turn”的函数。该函数应返回结构化结果，而不是用字符串 sentinel 作为跨层协议。至少增加以下回归测试：

1. `/command` 接收 `/skill course-report <task>` 后不得返回 `__SKILL_INVOKE__`。
2. 必须调用统一 `run_agent_turn()`，并传入显式 Skill 名称与任务。
3. Session 的 `skill_usage` 应记录 `source=explicit`。
4. Web UI 应展示最终回复、工具过程和 Approval，而非内部控制字符串。

### P2：中期报告证据陈旧

建议把测试数量改为自动生成或只写测试命令与最近验收日期，并链接本报告与机器可读结果，避免实现继续演进后文档失真。

### P3：增加跨入口合同测试

目前 CLI、Gateway 和 Scheduler 都复用 Agent Loop，但 command 层仍可能产生入口差异。建议为 `/session`、`/workspace`、`/compact`、`/skill`、`/auto`、`/unlimited` 建立参数化合同测试，验证 CLI 与 Gateway 的语义一致性。

## 7. 产物清单

- `evaluation/Claw验收任务设计.md`：任务、难度与评分规则。
- `evaluation/run_claw_acceptance.py`：隔离动态验收执行器。
- `evaluation/claw_acceptance_results.json`：13 项任务的机器可读结果。
- `claw_evaluation_workspace/`：合成材料与生成报告。
- `claw_evaluation_workspace/outputs/raft_course_report.md`：Skill + Tool + Approval 生成产物。
