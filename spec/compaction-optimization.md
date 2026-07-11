# 上下文压缩优化 v2 — Token 精确计数 + 窗口预算 + 异步压缩

> 版本: v1.0 | 日期: 2026-07-08 | 状态: 设计中

---

## 一、背景

### 1.1 当前实现的问题

v1 的上下文压缩（`claw/context/compaction.py`）存在三个结构性缺陷：

| 问题 | 症状 | 影响 |
|------|------|------|
| **字符数估算不准确** | `MAX_CHARS_BEFORE_COMPACTION = 4000` 作为 token 代理；中文 1 字符 ≈ 1.5-2 token，英文 4 字符 ≈ 1 token，偏差可达 2-3 倍 | 中文对话下可能已超 context window 仍未触发压缩 |
| **固定上下文未计入预算** | system prompt + soul + memory block + tool definitions + skill index 的 token 消耗完全被忽略，只统计了 `session.messages` 的字符数 | 8K 模型可能已被固定上下文占满，压缩阈值形同虚设 |
| **压缩阻塞对话** | `_maybe_auto_compact()` 同步等待 LLM 生成摘要，用户必须等待压缩完成后才能继续输入 | 长对话中每次压缩增加 2-5 秒延迟 |

### 1.2 为什么现在改

LLM 模型正在走向更大的 context window（32K → 128K → 1M），但 **上下文利用率** 而非窗口大小才是实际瓶颈。精确的 token 预算管理能让同一个 session 承载更多有效对话，减少不必要的压缩触发，同时在真正需要压缩时提前介入，避免信息丢失。

---

## 二、目标

### 2.1 本次要实现

**优化 #1 — Token 精确计数**：

1. 引入 `tiktoken` 库，实现跨模型兼容的 token 计数（优先 `o200k_base` 编码，回退字符估算）
2. `needs_compaction()` 改为基于 token 数判断，替代字符数阈值
3. 所有阈值（`MAX_MESSAGES_BEFORE_COMPACTION`、`KEEP_RECENT_MESSAGES`）改为 token 语义

**优化 #3 — 上下文窗口预算管理**：

1. 新增 `ContextBudget` 类，追踪每次 `build_messages()` 的完整 token 消耗
2. 可配置的 `MAX_CONTEXT_TOKENS`（默认值取模型窗口的 80%，留 20% 给模型输出）
3. 压缩触发条件从"消息数 OR 字符数"改为"消息 token 占窗口预算比例"
4. 在 API 调用前做 last-mile 检查：如果组装后的 `messages` 超出窗口上限，强行截断或报错

**优化 #5 — 异步压缩**：

1. 压缩在后台线程执行，对话循环不阻塞
2. 后台压缩期间用户可继续对话，新消息暂存、等待压缩完成后合并
3. 支持配置独立的压缩用 LLM（更便宜/更快），与主对话模型解耦
4. 压缩失败时不影响主对话流程，静默重试或放弃

### 2.2 非目标

- 不改变摘要的语义内容（不引入新的 prompt 策略，那是优化 #8）
- 不改变 `Session` 和 `Message` 的数据模型
- 不改变 `ContextBuilder.build_messages()` 的对外接口（签名保持兼容）
- 不改变 CLI 的 `/compact` 命令接口
- 不改变 Gateway API 接口
- 不引入消息重要性评分（那是优化 #6）
- 不引入压缩→记忆信息升级通道（那是优化 #7）

---

## 三、Token 计数方案

### 3.1 编码选择

优先使用 `tiktoken` 的 `o200k_base` 编码（GPT-4o / Claude 等主流模型的近似编码）。当 `tiktoken` 不可用时，回退到字符估算（保守估计：中文 1 字符 = 2 token，英文 4 字符 = 1 token）。

```python
# 伪代码
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:
    _ENC = None  # 回退字符估算

def count_tokens(text: str) -> int:
    if _ENC is not None:
        return len(_ENC.encode(text))
    # 回退：简单启发式
    cn_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - cn_chars
    return cn_chars * 2 + other // 4
```

### 3.2 计数范围

`needs_compaction` 只统计 `session.messages` 的内容 token（不包含 role 字符串），这与 v1 只统计消息内容的逻辑一致。`ContextBudget` 则统计**完整上下文**（见第四节）。

### 3.3 新增阈值

```python
# 替代旧阈值 — 均可通过环境变量覆盖
MAX_MESSAGE_TOKENS = int(os.getenv("COMPACT_MAX_MESSAGE_TOKENS", "2000"))
# 约 2000 token，等价于旧版约 4000 字符（中文场景下保守折算）

KEEP_RECENT_TOKENS = int(os.getenv("COMPACT_KEEP_RECENT_TOKENS", "1000"))
# 替代 KEEP_RECENT_MESSAGES=6，按 token 保留最近消息，
# 同时保留 KEEP_RECENT_MESSAGES_MIN=4 作为条数下限
```

---

## 四、上下文窗口预算管理

### 4.1 ContextBudget 类

新增 `claw/context/budget.py`，负责在 `build_messages()` 组装时追踪 token 消耗：

```python
@dataclass
class ContextBudget:
    max_tokens: int               # 窗口上限（可配，默认取模型窗口 * 0.8）
    system_prompt_tokens: int     # system prompt 固定消耗
    soul_tokens: int              # soul 固定消耗
    memory_block_tokens: int      # memory block 消耗（随记忆数量变化）
    tool_defs_tokens: int         # tool definitions 消耗（固定）
    skill_index_tokens: int       # skill index 消耗（随 skill 数量变化）
    summary_tokens: int           # compaction summary 消耗
    messages_tokens: int          # session.messages 总消耗
  
    @property
    def total_tokens(self) -> int: ...
    
    @property
    def available_tokens(self) -> int:
        """剩余可用 token 数。负值表示已超限。"""
        return self.max_tokens - self.total_tokens
    
    @property
    def usage_ratio(self) -> float:
        """0.0 ~ 1.0+，超过 1.0 表示已超限。"""
        return self.total_tokens / self.max_tokens
```

### 4.2 可配置的窗口大小

```bash
# .env 新增配置项
LLM_CONTEXT_WINDOW=32000           # 模型的上下文窗口大小（token）
LLM_CONTEXT_USAGE_RATIO=0.80       # 上下文利用率上限（触发压缩的阈值）
```

`MAX_CONTEXT_TOKENS = LLM_CONTEXT_WINDOW * LLM_CONTEXT_USAGE_RATIO`。默认值：窗口 32000，使用率 0.80，即预算 25600 token。

### 4.3 压缩触发条件变更

旧逻辑（v1）：
```
needs_compaction := len(messages) > 12 OR total_chars > 4000
```

新逻辑（v2）：
```
needs_compaction := (
    session.messages 的消息部分 token 数 > MAX_MESSAGE_TOKENS
    OR 全量上下文 usage_ratio > LLM_CONTEXT_USAGE_RATIO
)
AND len(messages) > KEEP_RECENT_MESSAGES_MIN
```

### 4.4 Last-mile 检查

在 `LLMClient._call_api()` 调用前，由 `ContextBudget` 做最终检查：

- `available_tokens > 0` → 正常发送
- `available_tokens <= 0 且 usage_ratio < 1.05` → 打印警告后发送
- `usage_ratio >= 1.05`（超出 5%）→ 抛出 `ContextOverflowError`，**不**发送请求（发送了模型也会截断前面的消息，不如在客户端处理）

此检查不替代触发压缩，只作为安全网。

### 4.5 按 token 保留最近消息

`compact_session` 的 `split_index` 不再按消息条数分割，而是按 token 数：

```python
def _find_split_index(messages: list[Message], keep_tokens: int) -> int:
    """从末尾向前累积 token 数，找到应保留的最早消息索引。"""
    accumulated = 0
    for i in range(len(messages) - 1, -1, -1):
        accumulated += count_tokens(messages[i].content)
        if accumulated >= keep_tokens:
            return i  # 从 i 开始保留
    return 0  # 全部保留
```

同时保留 `KEEP_RECENT_MESSAGES_MIN = 4` 作为绝对下限（至少保留 2 轮对话）。

---

## 五、异步压缩

### 5.1 执行模型

```
用户消息 → Agent Loop (不阻塞)
              ↓
         needs_compaction? → Yes → 后台线程执行 compact_and_persist
              ↓                              ↓
         继续下一轮对话              压缩完成 → 更新 session.summary
                                          ↓
                                    如果期间有新消息 → 新消息不参与本次压缩
```

### 5.2 CompactionWorker

新增 `claw/context/compaction_worker.py`：

```python
class CompactionWorker:
    """后台压缩执行器。"""
  
    def __init__(self, llm_client: LLMClient, compaction_llm_client: LLMClient | None):
        self._main_llm = llm_client
        self._compact_llm = compaction_llm_client or llm_client  # 可独立的廉价模型
        self._lock = threading.Lock()
        self._running = False
  
    def submit(self, session: Session, session_store: SessionStore) -> None:
        """提交压缩任务。如果已有任务在执行，跳过（不重复提交）。"""
        ...
  
    def wait(self, timeout: float | None = None) -> bool:
        """等待当前压缩任务完成。用于 session 退出/切换时确保落盘。"""
        ...
  
    def is_running(self) -> bool:
        """是否正在执行压缩。"""
        ...
```

### 5.3 压缩期间的并发处理

后台压缩执行期间可能发生：

| 场景 | 处理方式 |
|------|---------|
| 用户继续发送消息 | 新消息正常追加到 `session.messages`，不受影响 |
| 压缩完成、应用结果 | 新消息（在压缩启动后追加的）保持不变，只删除压缩启动时标记的旧消息 |
| 用户切换 session | `wait()` 等待压缩完成后再切换 |
| 程序退出 | `wait(timeout=5.0)` 等待最多 5 秒，超时则放弃 |
| 压缩 LLM 调用失败 | 静默重试一次；仍失败则记录警告，不阻塞主流程 |

### 5.4 快照式压缩

为避免竞争，`compact_session` 在工作线程启动时对 `session.messages` 做快照（浅拷贝列表引用），而不是锁住 session：

```python
def compact_session_snapshot(session: Session, llm_client: LLMClient) -> CompactionResult:
    """取快照后压缩，避免长时间持锁。"""
    with _lock:  # 短暂持锁
        snapshot_messages = list(session.messages)
        snapshot_summary = session.summary
  
    # 在锁外执行 LLM 调用（耗时操作）
    ...
  
    with _lock:  # 再次短暂持锁，应用结果
        # 只删除快照中的旧消息（新追加的不受影响）
        ...
```

### 5.5 独立压缩模型配置

```bash
# .env 新增配置项（可选，不配则复用主对话模型）
COMPACT_LLM_API_KEY=           # 默认复用 LLM_API_KEY
COMPACT_LLM_BASE_URL=          # 默认复用 LLM_BASE_URL
COMPACT_LLM_MODEL=             # 默认复用 LLM_MODEL
```

独立压缩模型的价值：
- 使用更便宜的模型（如 GPT-4o-mini）做摘要，大幅降低成本
- 避免主模型配额因压缩而被消耗
- 压缩调用不影响主模型的速率限制

---

## 六、实现改动点

### 6.1 核心改动

| 文件 | 改动 |
|------|------|
| `claw/context/token_counter.py` | **新增** — Token 计数工具（tiktoken + 字符回退） |
| `claw/context/budget.py` | **新增** — `ContextBudget` 类，追踪上下文 token 消耗 |
| `claw/context/compaction.py` | **重写阈值逻辑** — 从字符/消息数改为 token/窗口比例；按 token 保留最近消息 |
| `claw/context/compaction_worker.py` | **新增** — `CompactionWorker` 后台异步压缩 |

### 6.2 外围改动

| 文件 | 改动 |
|------|------|
| `claw/context/builder.py` | `build_messages()` 返回或附带 `ContextBudget`；`_build_summary_block` 不变 |
| `claw/config.py` | 新增 `LLM_CONTEXT_WINDOW`、`LLM_CONTEXT_USAGE_RATIO`、压缩模型配置的读取 |
| `claw/main.py` | 初始化 `CompactionWorker`，注入 `RuntimeState` |
| `claw/cli/repl.py` | `_maybe_auto_compact` 改为异步提交通道；退出时 `wait()` |
| `claw/cli/commands.py` | `/compact` 手动命令保持同步执行（用户明确等待） |
| `claw/gateway/server.py` | 同 REPL：自动压缩异步，`/compact` 保持同步 |
| `claw/agent/loop.py` | `run_agent_turn` 末尾触发 `_maybe_auto_compact`（统一入口，不再由 REPL/Gateway 各自触发） |
| `claw/llm/client.py` | `_call_api` 增加可选的 last-mile token 检查 |
| `requirements.txt` | 新增 `tiktoken>=0.5.0` |

### 6.3 不改动的文件

| 文件 | 原因 |
|------|------|
| `claw/session/models.py` | Session/Message 数据模型不变 |
| `claw/session/store.py` | 持久化逻辑不变 |
| `claw/tools/base.py` | 工具注册不变 |
| `claw/tools/memory_tools.py` | 记忆工具不变 |
| `claw/memory/store.py` | 记忆存储不变 |
| `claw/memory/reflection.py` | 记忆反思不变 |
| `claw/skills/registry.py` | Skill 系统不变 |
| `claw/scheduler/scheduler.py` | 调度器复用 agent loop，自动受益 |
| `claw/llm/protocol.py` | 协议解析不变 |

---

## 七、验收标准

### 7.1 Token 精确计数

1. **tiktoken 可用时**：`count_tokens("你好世界")` 返回精确 token 数，与 OpenAI tokenizer 页面结果一致
2. **tiktoken 不可用时**：回退字符估算，`count_tokens("你好hello")` 返回合理近似值
3. **`needs_compaction` 阈值语义变更**：12 条极短英文消息（如 "ok" × 12）不触发压缩；3 条很长中文消息触发压缩

### 7.2 上下文窗口预算

4. **`ContextBudget` 计算正确**：`build_messages()` 后 `budget.total_tokens` 覆盖所有组成部分
5. **配置生效**：设置 `LLM_CONTEXT_WINDOW=8000` + `LLM_CONTEXT_USAGE_RATIO=0.5` → 4000 token 时触发压缩
6. **Last-mile 安全网**：手动构建超长 `messages` 时，API 调用被拦截并给出清晰错误信息

### 7.3 异步压缩

7. **对话不阻塞**：自动压缩触发后用户立即可输入下一条消息
8. **并发安全**：压缩期间用户发送新消息，新消息不被删除
9. **退出等待**：程序退出时等待压缩完成（最多 5 秒），或超时放弃
10. **压缩失败不阻塞**：后台压缩 LLM 调用失败时，警告日志 + 下次对话继续尝试
11. **`/compact` 保持同步**：手动压缩命令仍阻塞等待结果并打印 summary
12. **独立模型可用**：配置 `COMPACT_LLM_MODEL=gpt-4o-mini` 后，压缩调用使用该模型

### 7.4 回归

13. **所有现有测试通过**（`test_core.py`、`test_reflection.py`、`test_step8_selfcheck.py`、`test_step9_selfcheck.py`）
14. **CLI / Gateway / Scheduler 三种入口均可正常触发压缩**

---

## 八、实现步骤

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1 | `requirements.txt` | 新增 `tiktoken>=0.5.0` |
| 2 | `claw/context/token_counter.py` | **新增** — `count_tokens()` + `count_tokens_for_messages()` |
| 3 | `claw/context/budget.py` | **新增** — `ContextBudget` 类 |
| 4 | `claw/config.py` | 新增 `LLM_CONTEXT_WINDOW`、`LLM_CONTEXT_USAGE_RATIO`、压缩模型配置 |
| 5 | `claw/context/compaction.py` | 重写 `needs_compaction()` 阈值逻辑；按 token 保留最近消息；新增 `compact_session_snapshot()` |
| 6 | `claw/context/compaction_worker.py` | **新增** — `CompactionWorker` 后台异步压缩 |
| 7 | `claw/context/builder.py` | `build_messages()` 增加 budget 计算和 last-mile 检查调用 |
| 8 | `claw/llm/client.py` | `_call_api` 增加可选的 token 溢出检查 |
| 9 | `claw/main.py` | 初始化 `CompactionWorker`，传入 `RuntimeState` |
| 10 | `claw/agent/loop.py` | 末尾统一触发异步压缩（替代 REPL/Gateway 各自触发） |
| 11 | `claw/cli/repl.py` | 删除独立的 `_maybe_auto_compact`；退出时 `worker.wait()` |
| 12 | `claw/gateway/server.py` | 同步骤 11 |
| 13 | `tests/` | 更新现有测试适配新阈值；新增 `test_compaction.py` 覆盖 token 计数和异步压缩 |
