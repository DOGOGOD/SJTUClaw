# Workspace 系统

## 概述

Workspace 系统为每个 session 提供独立的工作区沙箱。所有文件读写、Shell 命令执行都被限制在绑定的 workspace 目录内，防止 AI Agent 越权访问系统文件。

核心文件：
- `claw/workspace/manager.py` — WorkspaceManager，绑定管理与路径解析
- `claw/tools/readonly.py` — 只读工具（`list_dir`、`read_file`）
- `claw/tools/update.py` — 写工具（`create_file`、`edit_file`、`overwrite_file`）
- `claw/tools/shell.py` — Shell 工具（`new_shell`、`run_command`）
- `claw/tools/download.py` — 下载工具（`create_download`）
- `claw/tools/attachment.py` — 附件工具（`copy_attachment_to_workspace`）
- `claw/context/builder.py` — 上下文构建器（system prompt 中的 workspace 路径）

持久化文件：`data/workspace/bindings.json`

## 架构

```
                    WorkspaceManager
               (data/workspace/bindings.json)
               持久化 session_id → 路径 映射
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    ContextBuilder    Read-only Tools   Write/Shell Tools
    (系统提示中的     (list_dir,         (create_file, edit_file,
     workspace路径)   read_file)         new_shell, download...)
    有默认fallback    无绑定→裸路径      无绑定→拒绝执行
```

## 核心设计决策

### 1. 新建 session 不自动绑定 workspace

`create_session()` 不会自动调用 `workspace_manager.set()`。新 session 在 `WorkspaceManager` 中没有条目，`get()` 返回 `None`。

**影响：**
- **只读工具**（`read_file`、`list_dir`）：可正常使用，路径按原始输入解析（向后兼容，无沙箱）
- **写/Shell 工具**（`create_file`、`new_shell` 等）：拒绝执行，提示用户先使用 `/workspace set <路径>` 绑定工作区
- **系统提示**：使用默认 fallback 路径（Gateway 为项目根目录，CLI 为 data 目录）

### 2. 删除 session 时同步清理 workspace 绑定

两处 session 删除入口均已添加 workspace 清理：

| 入口 | 位置 | 清理方式 |
|------|------|----------|
| Gateway `DELETE /sessions/{id}` | `claw/gateway/server.py:1186` | `_workspace_manager.unset(session_id)` |
| CLI `/session delete <id>` | `claw/cli/commands.py:191-192` | `workspace_manager.unset(session_id)` |

`unset()` 会从内存和 `bindings.json` 中移除该 session 的绑定记录。

### 3. 重启后 workspace 绑定不丢失

`WorkspaceManager._load()` 在启动时从 `data/workspace/bindings.json` 恢复所有绑定。

**迭代前：** 恢复绑定时检查路径是否存在，若目录暂时不可达则静默丢弃绑定。
**迭代后：** 移除存在性检查，始终恢复所有绑定。若路径不可达，仅输出日志提示（`其中 N 个路径当前不可访问`），绑定本身不会丢失。

## WorkspaceManager API

```python
class WorkspaceManager:
    def set(session_id: str, path_str: str) -> Path
        # 绑定 session 到路径。验证路径存在且为目录。持久化到 bindings.json。

    def get(session_id: str) -> Path | None
        # 获取绑定的 workspace 路径，无绑定时返回 None。

    def unset(session_id: str) -> None
        # 解除绑定，从内存和 bindings.json 移除。

    def require(session_id: str) -> Path
        # 获取绑定的路径，无绑定时抛出 WorkspaceError。
        # 写/Shell 工具使用此方法——无绑定则拒绝执行。

    def resolve(session_id: str, path_str: str, *, must_exist=False) -> Path
        # 在 workspace 内安全解析相对路径。
        # 拒绝：绝对路径、路径遍历（../）、越界路径。
        # 可选检查目标是否存在。
```

## 工具行为矩阵

| 工具 | 有 workspace 绑定 | 无 workspace 绑定 |
|------|------------------|------------------|
| `read_file` | 路径解析到 workspace 内，绝对路径允许（需在范围内） | 使用裸路径（无沙箱） |
| `list_dir` | 同上 | 同上 |
| `create_file` | 路径解析到 workspace 内 | ❌ 拒绝，提示设置 workspace |
| `edit_file` | 同上 | ❌ 同上 |
| `overwrite_file` | 同上 | ❌ 同上 |
| `new_shell` | Shell 工作在 workspace 内 | ❌ 同上 |
| `run_command` | 命令在 workspace 沙箱中执行 | ❌ 同上 |
| `create_download` | 文件必须在 workspace 内 | ❌ 同上 |
| `copy_attachment` | 目标路径在 workspace 内 | ❌ 同上 |

## 安全边界

### 路径遍历防护
`resolve()` 方法通过 `Path.resolve().relative_to(ws)` 检测并拒绝路径遍历攻击：

```python
# 被拒绝的路径示例
"../out.txt"              # 路径遍历
"/etc/passwd"             # 绝对路径
"subdir/../../outside"    # 间接越界
```

### Shell 沙箱
Shell 工具（`run_command`）实现三层防护：
1. **前置检查**：扫描 `cd`/`pushd` 目标是否超出 workspace
2. **参数扫描**：检查已知文件操作命令（`rm`、`cp`、`mv` 等）的参数是否指向 workspace 外
3. **后置验证**：命令执行后捕获真实工作目录，若已越界则终止 Shell session

## CLI 命令

```
/workspace set <路径>    设置当前 session 的 workspace
/workspace show          查看当前 workspace
/workspace unset         取消 workspace 设置
```

## Gateway API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/workspace?sessionId=<id>` | 获取 session 的 workspace 信息 |
| POST | `/workspace` | 设置 workspace，body: `{sessionId, path}` |
| DELETE | `/workspace?sessionId=<id>` | 解除 workspace 绑定 |

## 线程安全

`WorkspaceManager` 所有 `_workspaces` 字典操作均持有 `threading.Lock`，支持 Gateway 的并发 HTTP + QQ + cron 线程同时存取。

## 文件结构

```
data/
└── workspace/
    └── bindings.json     # {"session_001": "/home/user/projects/foo", ...}
```

`bindings.json` 格式：
```json
{
  "session_001": "C:\\Users\\GZQ\\projects\\myapp",
  "session_002": "/home/user/workspace"
}
```
