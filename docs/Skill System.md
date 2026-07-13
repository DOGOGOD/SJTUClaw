# Skill System — 迭代更新记录

## 一、最终架构

Skill 系统采用 **LLM 工具驱动 + 渐进式披露** 的设计。LLM 通过三个工具自主发现、加载和管理 Skill：

```
skills_list()           → 浏览可用 Skill（name + description）
skill_view(name)        → 加载完整 SKILL.md（含子文件访问）
skill_manage(action, …) → 创建/编辑/删除 Skill（写操作，经过审批）
```

所有 Skill 存放在 `skills/` 目录，每个 Skill 是一个子目录，包含 `SKILL.md`（YAML frontmatter + Markdown 正文）和可选的 `references/`、`templates/`、`assets/` 子目录。

---

## 二、迭代过程

### 第 1 轮：统一目录（移除双层加载）

**原状**：代码定义了两个 Skill 路径——`skills/`（用户 Skill）和 `claw/skills_builtin/`（内置 Skill），后者目录不存在，Tier 1 加载实际被跳过。

**改动**：
- 移除 `BUILTIN_SKILLS_DIR` 常量和所有 builtin 引用
- `SkillRegistry.__init__()` 移除 `builtin_dir` 参数
- `_scan()` 和 `rescan()` 统一为单目录扫描
- `SkillInfo.source` 硬编码为 `"workspace"`

### 第 2 轮：从审批驱动改为工具驱动

**原状**：每次 LLM 调用时 Context Builder 将 Skill 索引注入 system message；LLM 通过 `use_skill` 工具请求激活 Skill → 创建审批 → 用户确认 → Skill 内容注入 session。同时存在一个从未使用的 `SkillMatcher`（Jaccard 算法评分）。

**改动**：
- **删除** `claw/tools/skills.py`（`use_skill` 工具）
- **删除** `claw/skills/matcher.py`（`SkillMatcher` / `SkillComposer`，从未使用）
- **新建** `claw/tools/skills_tool.py`——`skills_list` 和 `skill_view` 两个只读工具
- **Agent Loop** 移除 `skill_registry` / `skill_source` / `skill_name` 参数；移除 `_SKILL_SELECT_LEVEL` 常量；删除 `_handle_skill_select()` / `_record_skill_usage()` / `_update_skill_output()` 三个函数
- **Context Builder** 移除 `_build_skill_block()` / `build_skill_injection_message()`，不再将 Skill 索引注入每轮上下文
- **Gateway** 移除 `/skill` 斜杠命令和 `/skills` REST 端点
- **CLI** 移除 `/skill` 命令和 `_handle_skill_invoke()` 函数

### 第 3 轮：清理冗余

**原状**：第 2 轮改动后残留了多处旧系统的死代码。

**改动**：
- `agent/turn_context.py`——移除 `skill_source` / `skill_name` / `auto_reason` / `skill_already_injected` 字段及相关属性
- `agent/metrics.py`——移除 `skill_injections` / `skill_auto_selected` 字段和 `record_skill_injection()` 方法
- `skills/registry.py`——移除 `SkillUsageRecord` 类（仅被已删除的审批流程使用）
- `context/builder.py`——移除 `set_skill_registry()` 空桩
- `skills/__init__.py`——移除 `SkillUsageRecord`、`SkillMatcher` 导出
- 清理过期 pycache

### 第 4 轮：CLI 注册 Skill 工具

**原状**：CLI 模式未注册 Skill 工具，与 Gateway 不一致。

**改动**：
- `claw/main.py`——创建 `SkillRegistry` 实例并传入 `run_repl()`
- `claw/cli/repl.py`——接受 `skill_registry` 参数，在有 workspace 和无 workspace 两条路径都注册三个 Skill 工具；启动时打印 Skill 数量

### 第 5 轮：新增 skill_manage 工具

**原状**：Skill 系统只读——LLM 可以浏览和读取 Skill，但无法创建或修改。

**改动**：
- **新建** `claw/tools/skill_manager_tool.py`——6 个操作：create / edit / patch / delete / write_file / remove_file
- `safety_level = "write"`——经过审批系统
- 名称校验（正则 `[a-z0-9][a-z0-9._-]*`，≤64 字符）
- Frontmatter 格式校验（`---` 开头/结尾、正文非空）
- 路径越界防护（`..` 拒绝 + `resolve().relative_to()` 双重检查）
- 原子写入（tmp + replace）
- 删除不可逆——移动到 `skills/.archive/`，时间戳防冲突
- 支持分类目录（`category` 参数创建 `skills/<category>/<name>/`）
- 关联 `SkillRegistry.rescan()`——create/edit/delete 后即时同步缓存

### 第 6 轮：一致性修复

**问题**：
1. `_find_skill_dir` 只搜索一层，分类 Skill 无法被后续操作找到
2. `replace_all=True` 时无匹配静默写入
3. category 名称可能与已有 Skill 目录冲突

**修复**：
- 两层搜索（平层 + category 子目录），排除 `.archive/`
- `replace_all` 路径添加 `count==0` 错误处理
- 创建时检查 category 是否与已有 Skill 同名
- `create_skill_manage_tool(registry)` 接受 registry 参数以支持 rescan

---

## 三、最终文件清单

```
claw/skills/
├── __init__.py          # 导出 SkillRegistry, SkillInfo, SkillUsageStore
├── registry.py          # 扫描/加载/格式化 SKILL.md，usage 统计，热重载
└── usage.py             # SkillUsageStore（use/view/patch 计数器）

claw/tools/
├── skills_tool.py       # skills_list + skill_view（只读，并发安全）
├── skill_manager_tool.py # skill_manage（写操作，经过审批）
└── __init__.py          # 注册逻辑

skills/                  # 数据目录
├── course-report/SKILL.md
├── material-summary/SKILL.md
└── presentation-outline/SKILL.md
```

---

## 四、工具索引

| 工具 | safety_level | 并发安全 | 描述 |
|------|-------------|---------|------|
| `skills_list` | read_only | ✅ | 列出可用 Skill（name + description） |
| `skill_view` | read_only | ✅ | 加载 SKILL.md 全文或子文件 |
| `skill_manage` | write | ❌ | 创建/编辑/删除 Skill（6 个 action） |

### skill_manage action 参考

| action | 必需参数 | 说明 |
|--------|---------|------|
| `create` | name, content | 新建 Skill 目录 + SKILL.md |
| `edit` | name, content | 全文替换 SKILL.md |
| `patch` | name, old_string, new_string | 精准查找替换 |
| `delete` | name | 归档到 .archive/ |
| `write_file` | name, file_path, file_content | 写入 references/templates/assets 子文件 |
| `remove_file` | name, file_path | 删除子文件 |
