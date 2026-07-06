# SJTUClaw

一个最小、面向教学的 LLM agent 运行时。当前已实现 Step 0~4（阶段 1、阶段 2、阶段 3）：

- 从 `.env` / 环境变量读取 LLM 配置，并在缺失时给出清晰提示。
- 最小 LLM API 调用封装（`claw/llm/client.py`），统一处理网络异常、HTTP 状态异常、
  响应格式异常。
- 终端多轮对话循环（`claw/cli/repl.py`）。
- 多 session 管理与持久化：可创建、列出、切换、重命名、删除 session，程序重启后
  历史不丢失。
- System Prompt、Soul、Memory 三类稳定上下文（stable context），与普通 session
  历史（conversation context）严格分离。
- Compaction 上下文压缩：session 历史过长时自动把较早消息压缩为 session summary，
  只保留最近若干轮原始消息；也支持 `/compact` 手动触发。

## 目录结构

```
SJTUClaw/
  .env.example        # LLM 配置模板，复制为 .env 后填写真实值
  .gitignore           # 忽略 .env、data/ 等运行产物
  requirements.txt
  README.md
  prompts/
    system_prompt.md   # 系统规则，程序启动时加载，不可被对话覆盖
    soul.md             # claw 的稳定人格与语气，程序启动时加载，不可被对话覆盖
  data/                 # 运行期产生的数据，已被 .gitignore 排除
    sessions/<sessionId>/session.json   # 每个 session 独立一个文件
    memory/memory.json                   # 跨 session 的长期记忆
  claw/
    config.py           # 读取 .env / 环境变量 + 关键路径常量，配置缺失时报清晰错误
    prompts.py           # 加载 prompts/system_prompt.md、prompts/soul.md
    llm/
      client.py          # 封装 chat(messages) -> assistant 文本
    session/
      models.py           # Session、Message 数据结构
      store.py             # SessionStore：创建/列出/切换/重命名/删除/持久化
    memory/
      store.py             # MemoryStore：add/list/delete，持久化到 memory.json
    context/
      builder.py           # ContextBuilder：拼装 system prompt -> soul -> memory
                            # -> session summary -> 最近 session 消息
      compaction.py         # Compaction：触发判断 + 用 LLM 生成/合并 session summary
    cli/
      commands.py          # /session、/memory、/compact 等内部命令解析与处理
      repl.py               # 终端多轮对话循环（只负责输入输出，每轮后检查是否需要压缩）
    main.py                # 程序入口：python -m claw.main
```

**职责边界**：`session` 只管存取历史；`memory` 只管长期事实；`context.builder`
是唯一负责把“存储结构”拼装成“LLM 输入结构”的地方；`context.compaction` 只负责压缩
`session.messages`/`session.summary`，不读不写 system prompt、soul、memory；`cli`
只负责终端输入输出和命令分发，不直接拼 messages、不直接读写 session/memory 文件。

## 环境准备

1. 安装 Python 3.9+（推荐使用虚拟环境，例如 `venv` 或 conda）。
2. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

## 配置 `.env`

1. 复制配置模板：

   ```bash
   cp .env.example .env
   ```

   （Windows PowerShell 可用 `copy .env.example .env`）

2. 编辑 `.env`，填入你的真实配置：

   ```
   LLM_API_KEY=你的密钥
   LLM_BASE_URL=你的模型服务地址，例如 https://models.sjtu.edu.cn/api/v1
   LLM_MODEL=你要使用的模型名称，例如 glm
   ```

3. `.env` 已被 `.gitignore` 排除，**不会**被提交到 Git；也可以不使用 `.env`，改为直接
   设置同名的系统环境变量。

> 警告：不要把 API Key 写死在代码里，也不要出现在 README、commit、Issue 或截图中。

## Prompts 配置（System Prompt / Soul）

- `prompts/system_prompt.md`：系统规则和行为边界（偏“做什么、不能做什么”）。
- `prompts/soul.md`：claw 的稳定人格、语气和交互风格（偏“怎么说话”）。

两者都在程序启动时从文件加载一次，每次调用 LLM 都会带上；**不会**因为某一轮普通
对话内容而被修改或覆盖，只能通过编辑这两个文件 + 重启程序来调整。

## 启动

固定启动命令（在本目录，即 `SJTUClaw/SJTUClaw/` 下执行）：

```bash
python -m claw.main
```

运行效果示例：

```
$ python -m claw.main

claw started. Type /exit to quit.
Current session: default

User> 你好，我叫小明。
Assistant> 你好，小明喵！很高兴认识你喵。

User> 我刚才说我叫什么？
Assistant> 你刚才说你叫小明喵。

User> /exit
bye.
```

- 输入 `/exit` 可正常退出。
- 按 `Ctrl+C` 或 `Ctrl+D` 会优雅退出，不会打印异常堆栈。
- 若未配置 `.env` 或环境变量缺失，或 `prompts/` 下的文件缺失/为空，程序会打印清晰的
  错误提示并以非 0 状态码退出，不会打印裸异常堆栈。
- 若网络不通、Base URL 错误或 API Key 无效，对话过程中会打印清晰的错误提示，程序不会
  崩溃，可以继续下一轮输入。

## 内部命令

以下命令由 CLI 直接拦截处理，**不会**作为普通消息发送给 LLM。

### Session 管理

```
/session new
  创建一个新的 session，并切换到该 session。

/session list
  列出所有已有 session（sessionId、title、消息数量、更新时间）；
  当前所在的 session 前面会标 `*`。

/session switch <sessionId>
  切换到指定 session，之后的对话只使用该 session 的历史。

/session rename <sessionId> <title>
  修改指定 session 的标题。

/session delete <sessionId>
  删除指定 session。若删除的是当前 session，会自动切换到另一个
  （或自动新建一个默认 session）。
```

示例：

```
User> /session list
Sessions:
* default    默认会话    messages=0    updated=2026-07-06T08:00:00+00:00

User> /session new
Created session: session_001

User> /session switch default
Switched to session: default
```

### Memory 管理（跨 session 的长期记忆）

```
/memory add <content>
  添加一条长期记忆，例如用户的长期偏好、项目背景。

/memory list
  列出所有 memory（memoryId + 内容）。

/memory delete <memoryId>
  删除一条 memory。
```

示例：

```
User> /memory add 用户正在实现一个名为 claw 的课程 agent 项目。
Added memory: mem_001

User> /session new
Created session: session_001

User> 我现在在做什么项目？
Assistant> 你正在实现一个名为 claw 的课程 agent 项目喵。
```

memory 一旦写入，在所有 session 中都可见，不依赖某个 session 的历史消息；只能通过
`/memory add`、`/memory delete` 修改，不会被普通对话自动改写。

### Compaction 上下文压缩

随着对话变长，claw 会自动把较早的 session 消息压缩成 `session.summary`，只保留最近
若干条原始消息。**Compaction 只处理当前 session 的 `summary` 和 `messages`，完全不读不写
 system prompt、soul 配置或 memory。**

触发阈值（定义在 `claw/context/compaction.py`），满足任一条即触发：

```
KEEP_RECENT_MESSAGES = 6            # 永远保留最近 6 条原始消息（约 3 轮对话），不参与压缩
MAX_MESSAGES_BEFORE_COMPACTION = 12  # 消息总数 > 12 就触发（不管长短）
MAX_CHARS_BEFORE_COMPACTION = 4000   # 所有消息字符总数 > 4000 就触发
```

选择理由：

- 精确的 token 数依赖具体模型的 tokenizer，claw 拿不到，所以用**字符数**作为一个简单、
  可解释的代理指标：中英混合文本中 1 token 大约对应 1.5~2 个字符，4000 字符大约对应
  2000~3000 token，在主流 8K+ 上下文窗口中留有足够余量给 system prompt/soul/memory 和
  模型回复。
- **消息数量**阈值是一个独立的安全网，防止大量短消息把历史把消息数拉得很高、但字符数还
  没超阈的情况。
- `KEEP_RECENT_MESSAGES` 保证最近一次交互总是以原始消息形式保留，不会被压缩掉细节。

压缩流程：

1. 把较早的消息（除最近 `KEEP_RECENT_MESSAGES` 条以外的部分）交给 LLM，要求它结合已有
   `session.summary` 生成一份**合并后**的新摘要（保留当前任务、已完成内容、用户明确要求/
   偏好/约束、未解决问题、影响后续回答的关键事实；删除寒暄、重复、无关细节）。
2. 只有当 LLM 返回非空的有效摘要时，才会用新 summary 替换旧 summary、并把旧消息从
   `session.messages` 中删除，只保留最近 `KEEP_RECENT_MESSAGES` 条。
3. 处理失败时的保护：
   - **LLM 调用失败**（网络/HTTP/格式异常）：不会删除任何旧消息，`session` 保持原样。
   - **返回的 summary 为空/无效**：不应用本次压缩结果，`session` 保持原样。
   - **压缩本身成功但保存失败**：内存中的 `session` 已更新，但会提示用户本次结果可能没有
     成功落盘（重启后可能丢失这次压缩）。
   - 普通对话轮次中，若 LLM 调用失败，**不会**向 session 追加空的 assistant 消息。
4. 压缩成功后（无论自动还是手动）都会打印压缩结果和更新后的 summary 预览。

自动触发示例（发生在每轮对话结束后）：

```
User> 我们继续完善 claw 的 Lab 文档。
Assistant> 好的，我们继续。

...

[system] compact session session_001: old_messages=13, recent_messages=6
[system] summary:
当前任务：完善 claw 的 Lab 文档。
已完成：Step 0 到 Step 3 的核心能力。
下一步：继续完善 Step 4 的 compaction 行为。
```

也可以用 `/compact` 手动立即触发当前 session 的压缩，方便调试和验收：

```
User> /compact
Compacted session session_001.
Old messages: 13
Recent messages: 6
Summary updated: yes
Summary:
当前任务：完善 claw 的 Step 4 文档。
已完成：说明 compaction 的边界、触发策略和失败保护。
下一步：验证 context builder 能正确使用 session summary。
```

若当前 session 消息数不超过 `KEEP_RECENT_MESSAGES`，`/compact` 会直接提示无需压缩，不会
白白调用 LLM。

## 数据存放位置

```
data/sessions/<sessionId>/session.json   # 每个 session 一个独立文件，可按 id 直接定位
                                           # 字段包括 sessionId/title/messages/summary/
                                           # createdAt/updatedAt
data/memory/memory.json                   # 跨 session 的长期记忆
```

- `data/` 已被 `.gitignore` 排除，不会提交到 Git。
- 保存失败时（例如磁盘写入错误）会打印清晰的 `[错误]` 提示，不会静默丢失。
- 若 `session.json` 或 `memory.json` 损坏、无法解析为合法 JSON，程序不会静默丢弃
  数据：会把损坏文件重命名备份为 `*.corrupted-<时间戳>`，打印警告说明详情，并以一个
  空白/默认状态继续运行，原始损坏文件仍保留在磁盘上以便排查。
