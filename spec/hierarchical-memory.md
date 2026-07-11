# 层次化记忆系统 v2 — Markdown 文件存储（Hermes 风格）

> 版本: v2.0 | 日期: 2026-07-08 | 状态: 实现中

---

## 一、背景

### 1.1 为什么要重构

v1 用单个 JSON 文件存储所有记忆，存在三个固有问题：

| 问题 | 症状 |
|------|------|
| **不可读** | 用户无法直接打开文件查看/编辑记忆，必须通过 CLI |
| **中文检索失效** | `"我是谁"` 与 `"我是张三"` 无公共子串，recall 得分 0 |
| **单点故障** | 整个 memory.json 损坏 → 全部记忆丢失 |
| **难以版本控制** | 一个 JSON 文件改了任何记忆都是整文件 diff |

### 1.2 Hermes 风格记忆系统

Hermes 类记忆系统的核心原则：**文件系统即数据库**。每条记忆是一个独立的 Markdown 文件，使用 YAML frontmatter 存储结构化元数据，正文存储富文本内容。这与项目已有的 Skill 系统（`SKILL.md`）格式完全一致。

---

## 二、目标

### 2.1 本次要实现

用 **Markdown 文件** 替代 `data/memory/memory.json` 作为记忆的持久化存储，同时：

1. **一条记忆 = 一个 .md 文件** — 分类目录组织，frontmatter + markdown body
2. **保持内存索引** — 启动时扫描所有 .md 建索引，recall 不走磁盘
3. **保持所有上层接口不变** — Agent 工具、CLI 命令、Gateway API 全部兼容
4. **自动迁移旧数据** — 首次启动检测到旧 memory.json 时迁移为 .md 文件
5. **中文检索增强** — 已添加 CJK 字符级匹配，本次确保与 Markdown 正文协作良好

### 2.2 非目标

- 不改为向量检索
- 不改变 Agent 工具接口（`remember` / `recall` 参数不变）
- 不改变 CLI 命令接口
- 不改变 Gateway API 接口
- 不引入新依赖

---

## 三、数据格式

### 3.1 目录结构

```
data/memory/
├── user_preference/
│   └── 语言偏好-中文交流.md
├── project/
│   └── 智能客服系统.md
├── fact/
│   ├── 用户身份-张三.md
│   └── 工作目录配置.md
├── decision/
│   └── 数据库选型-PostgreSQL.md
└── general/
    └── 杂项记录.md
```

### 3.2 单文件格式

```markdown
---
id: mem_001
category: fact
tags:
  - identity
  - user-info
  - school
importance: 4
source_session_id: session_001
created_at: "2026-07-08T10:00:00"
updated_at: "2026-07-08T10:30:00"
last_recalled_at: "2026-07-08T12:00:00"
recall_count: 5
---

# 用户身份

用户是张三，上海交通大学计算机科学专业大三学生。

- 学号：521030910001
- 导师：李教授
- 校区：徐汇校区
```

### 3.3 Frontmatter 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 唯一标识，格式 `mem_NNN` |
| `category` | string | 是 | 枚举值 |
| `tags` | string[] | 否 | 标签列表 |
| `importance` | int | 否 | 1-5，默认 3 |
| `source_session_id` | string | 否 | 来源 session |
| `created_at` | string | 否 | ISO 时间戳 |
| `updated_at` | string | 否 | ISO 时间戳 |
| `last_recalled_at` | string | 否 | 最后检索时间 |
| `recall_count` | int | 否 | 被检索次数 |

### 3.4 Frontmatter 的 tags 格式

支持 YAML 原生数组（推荐）和 JSON 风格数组两种写法：

```yaml
# YAML 数组（推荐）
tags:
  - python
  - fastapi

# JSON 风格（兼容）
tags: [python, fastapi]
```

### 3.5 文件名规则

从 content 前 50 字生成 URL-safe slug，示例：
- `"用户是张三，上海交大学生"` → `用户是张三-上海交大学生.md`
- `"项目使用FastAPI+PostgreSQL"` → `项目使用fastapi-postgresql.md`

文件名冲突时追加数字后缀：`用户是张三-上海交大学生-2.md`

---

## 四、MemoryStore 接口（保持不变）

上层所有调用方（CLI、Gateway、Agent 工具、Reflection）**零改动**。

```python
class MemoryStore:
    def __init__(self, memory_dir: Path): ...
    
    # 基础 CRUD
    def list(self) -> list[MemoryEntry]: ...
    def add(self, content, *, category, tags, importance, source_session_id) -> MemoryEntry: ...
    def update(self, memory_id: str, content: str) -> MemoryEntry: ...
    def delete(self, memory_id: str) -> None: ...
    
    # 结构化操作
    def list_by_category(self, category: str | None) -> list[MemoryEntry]: ...
    def stats(self) -> dict[str, int]: ...
    def recall(self, query: str, category: str | None, limit: int) -> list[MemoryEntry]: ...
```

---

## 五、实现改动点

### 5.1 核心：`claw/memory/store.py` — 完全重写

**加载**：
1. 扫描 `data/memory/` 下所有 `*/*.md` 文件
2. 用 YAML frontmatter 解析器提取元数据 + markdown body
3. 构建内存中的 `list[MemoryEntry]`
4. 如果发现旧 `memory.json`（无 .md 文件但有 JSON），自动迁移

**写入（add）**：
1. 生成 memory_id
2. 构建 frontmatter + markdown body
3. 确保分类目录存在
4. 生成文件名 slug
5. 原子写入 .md 文件（tmp+replace）
6. 追加到内存列表

**更新（update）**：
1. 找到对应 .md 文件
2. 重写文件（更新 content + updated_at）
3. 更新内存中的条目

**删除（delete）**：
1. 删除对应 .md 文件
2. 从内存列表中移除
3. 如果分类目录为空，可选择清理

**检索（recall）**：
- **不变** — 仍然基于内存中的 `list[MemoryEntry]` 做关键词+标签+CJK字符打分

### 5.2 Frontmatter 解析

复用 Skill 系统已有的 frontmatter 解析逻辑（`claw/skills/registry.py` 中的 `_parse_frontmatter`）。如果格式差异大，在 `memory/store.py` 中独立实现轻量版。

实现方案：不使用 PyYAML 依赖。手工解析 YAML frontmatter（`---` 分隔，逐行解析 key: value），与 `SKILL.md` 解析方式一致。

### 5.3 其他文件改动

| 文件 | 改动 |
|------|------|
| `claw/memory/store.py` | **重写** — 基于 .md 文件的 MemoryStore |
| `claw/config.py` | `MEMORY_FILE` → `MEMORY_DIR` |
| `claw/main.py` | 传 `MEMORY_DIR` 而非 `MEMORY_FILE` |
| `claw/gateway/server.py` | 同上 |
| `claw/memory/reflection.py` | 传 `MEMORY_DIR` |
| `tests/test_core.py` | memory 测试改用目录而非文件 |
| `tests/test_reflection.py` | 同上 |

### 5.4 不改动的文件

| 文件 | 原因 |
|------|------|
| `claw/tools/memory_tools.py` | Agent 工具接口不变 |
| `claw/tools/__init__.py` | 注册逻辑不变 |
| `claw/context/builder.py` | 上下文注入逻辑不变 |
| `claw/cli/commands.py` | CLI 命令不变 |
| `claw/agent/loop.py` | Agent 循环不变 |

---

## 六、旧数据迁移

启动时检测逻辑：

```
if memory.json 存在 AND data/memory/{category}/*.md 数量 == 0:
    1. 读取 memory.json
    2. 对每条 entry 生成 .md 文件，写入对应分类目录
    3. 将 memory.json 重命名为 memory.json.migrated
    4. 打印迁移日志
```

迁移后每条记忆的 .md 文件包含 frontmatter（所有字段）+ 正文（从 content 字段）

---

## 七、验收标准

1. **创建记忆** → `data/memory/fact/xxx.md` 文件出现
2. **删除记忆** → 对应 .md 文件消失
3. **更新记忆** → .md 文件的正文和 `updated_at` 被更新
4. **recall 检索** → 与 v1 行为一致
5. **旧数据迁移** → 启动后 memory.json 变为 memory.json.migrated，出现 .md 文件
6. **文件直接编辑** → 用户在编辑器中修改 .md 的 body 内容，重启后生效
7. **所有现有测试通过**

---

## 八、实现步骤

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1 | `claw/memory/store.py` | 完全重写：基于目录扫描 + frontmatter 解析 + .md 读写 |
| 2 | `claw/config.py` | `MEMORY_FILE` → `MEMORY_DIR` |
| 3 | `claw/main.py` | 传目录路径 |
| 4 | `claw/gateway/server.py` | 传目录路径 |
| 5 | `claw/memory/reflection.py` | 传目录路径 |
| 6 | `tests/test_core.py` | 适配新接口 |
| 7 | `tests/test_reflection.py` | 适配新接口 |
