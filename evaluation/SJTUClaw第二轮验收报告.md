# SJTUClaw 第二轮高难度验收报告

## 1. 总结

第二轮共执行 **25 项动态任务，25 项全部通过**：

- `SJTUClaw.md` Step 0—Step 9 升级任务：13/13。
- 项目新增功能任务：12/12。
- 后端全量回归：399 项及 2 项子测试通过。
- 前端回归：9 个测试文件、41 项测试通过。
- Web UI 生产构建：成功，转换 2122 个模块。

按照课程原百分制重新评分为 **98/100**：课程功能 80/80、代码质量与整体完成度 10/10、中期报告 8/10。报告项扣分主要因为尚未自动链接最新验收产物，且批量任务未提供外部真实模型自由规划的演示证据；Windows Session 临时文件替换警告另列为持续风险。

## 2. 本轮修复

### 2.1 Gateway/CLI 显式 Skill 调用

第一轮缺陷表现为 `/skill course-report <task>` 把内部字符串 `__SKILL_INVOKE__|...` 直接返回给 Web 客户端。修复后：

- CLI 和 Gateway 都会解析并消费内部调用信号。
- Gateway 通过统一 `run_agent_turn()` 执行任务。
- 正确传入 `skill_registry`、`skill_source="explicit"` 与 `skill_name`。
- 继续复用 Approval、AUTO/UNLIMITED、Workspace 回退、取消事件和桌宠状态。
- 模型未配置时返回配置提示，不再泄露内部字符串。
- T09E 第二轮通过。

### 2.2 停止期间的迟到模型回复

新增高难度任务 N11 发现：同步模型请求进行中调用 `/stop` 后，如果模型随后返回最终文本，旧逻辑可能直接接受该文本。修复后 Agent 在模型调用返回后、处理 final/tool call 前再次检查取消事件：

- 迟到最终回复被忽略。
- Session 写入明确的“已终止”简报。
- 已经完成的工具结果仍保留。
- 同 Session 并发请求继续返回 409。

## 3. 课程功能结果

| 评分项 | 得分 | 第二轮证据 |
| --- | ---: | --- |
| Step 0 | 10/10 | 配置、真实模型冒烟、错误提示与本地调用合同通过 |
| Step 1 | 10/10 | 历史依赖计算正确，缺历史时测试模型不会放行 |
| Step 2 | 10/10 | Session 隔离、命令路由、重命名和 Store 重载通过 |
| Step 3 | 10/10 | Memory 写入、跨 Session 注入、搜索通过 |
| Step 4 | 10/10 | 超预算压缩、摘要重载、早期事实保留通过 |
| Step 5 | 10/10 | 目录、并行读文件、observation 后继续推理通过 |
| Step 6 | 5/5 | Gateway、附件 Session 隔离、前端测试与构建通过 |
| Step 7 | 5/5 | 一次性任务和新增周期任务均通过 |
| Step 8 | 5/5 | 批准、拒绝、AUTO/UNLIMITED 安全矩阵通过 |
| Step 9 | 5/5 | 自动加载、引用资源、显式 Gateway 调用和 Skill 生命周期通过 |
| **课程功能** | **80/80** | Step 0—9 全部形成真实 Runtime 闭环 |

代码质量与整体完成度评为 10/10：入口复用统一 Agent Loop，新增回归覆盖了上一轮遗漏的跨入口合同和取消竞态，后端 399 项全部通过。中期报告评为 8/10：结构完整且本轮已更新测试数字和章节编号，但仍缺少自动生成的验收结果链接及真实外部模型的批量演示证据。

## 4. 新增功能结果

| 能力组 | 结果 | 关键证据 |
| --- | --- | --- |
| AUTO/UNLIMITED | 3/3 通过 | AUTO 内部写入 0 审批；越界拒绝；UNLIMITED+AUTO 强制审批 |
| Workspace 回退 | 通过 | 预览显示恢复 1 文件、删除 1 文件、移除 5 条消息；Undo 恢复 2 文件及完整对话 |
| Reflection | 通过 | 审阅 3 个 Session，抽取 1 条 decision，搜索召回成功 |
| Heartbeat | 通过 | Active Tasks 触发“发布清单仍有未完成项”告警 |
| 周期任务 | 通过 | 成功执行 2 次，禁用后历史不再增长，删除成功 |
| 设置安全 | 通过 | QQ Secret 仅以 Fernet 密文保存，API 返回掩码；非法头像为 400 |
| 桌宠安全 | 通过 | 3 个内置宠物可列出；恶意 ZIP 路径穿越返回 400 |
| Web 安全 | 通过 | `127.0.0.1` 在请求前被识别为非公网地址 |
| 并发与停止 | 通过 | 并发请求 409，`/stop` 取消 1 个任务，迟到回复被丢弃 |
| Skill 生命周期 | 通过 | 上传、热加载、详情、删除全部为 200，删除后无残留 |

## 5. 仍需关注的风险

1. **Windows Session 原子替换警告**：课程基线运行结束时再次出现一次自动标题保存临时文件 `replace` 的 `WinError 5`。主任务与 Session 内容未丢失，但说明自动标题、后台压缩或并发保存之间仍可能发生 Windows 文件占用竞争。建议统一让 Session Store 的所有写入经过同一 per-session 锁，并增加 Windows 故障注入测试。
2. **真实模型证据边界**：为避免未经确认批量外发内置 prompt、Skill 文档和测试材料，25 项自动验收使用本地确定性模型提出 Tool Call；真实模型只保留单条冒烟证据。Runtime 闭环得到充分验证，外部模型的自主选 Skill 与长任务规划质量仍应在答辩现场补充演示。
3. **桌面与 QQ 外部依赖**：本轮验证了配置、加密、目录、安全校验和回调合同，没有启动真实桌面窗口或连接 QQ 公网。

## 6. 交付证据

- `evaluation/Claw第二轮高难度验收任务设计.md`
- `evaluation/run_claw_acceptance.py`
- `evaluation/run_claw_acceptance_round2.py`
- `evaluation/claw_acceptance_results.json`（课程功能 13/13）
- `evaluation/claw_acceptance_round2_results.json`（新增功能 12/12）
- `tests/test_explicit_skill_entrypoints.py`
- `tests/test_cancel_turn.py`
- `claw_evaluation_workspace/`
- `claw_evaluation_workspace_round2/`
