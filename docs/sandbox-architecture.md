# microsandbox 接入架构

## 目标

这层接入同时解决两个问题：

1. 没有设置 workspace 时，原生 Agent 仍能立即获得可写、可执行的工作目录；
2. 执行不可信命令时，边界由 microVM 提供，而不再只依赖路径字符串检查。

## 会话数据流

```mermaid
flowchart LR
    LLM["LLM 原生工具调用"] --> Router["SandboxManager 路由"]
    Router -->|"未绑定 workspace"| Private["session 私有命名卷"]
    Router -->|"已绑定 workspace"| Bind["只挂载明确绑定的宿主目录"]
    Private --> VM["microsandbox microVM<br/>/workspace"]
    Bind --> VM
    VM --> Shell["Linux /bin/sh"]
    VM --> Files["read/list/create/overwrite/edit"]
    Upload["当前 session 附件"] -->|"copy_from_host"| VM
    VM -->|"copy_to_host"| Export["受管导出目录"]
    Export --> Download["限时 downloadId"]
```

每个 SJTUClaw session 最多拥有一个运行中的 microVM。Shell 与结构化文件工具
复用同一实例，因此 `run_command` 生成的文件能立即被 `read_file` 和
`create_download` 看见。异步 microsandbox SDK 固定运行在专用事件循环线程上，
同步 Tool handler 通过线程安全 Future 调用，避免跨事件循环复用 SDK 对象。

Shell 输出通过 SDK 事件流持续排空，不会先在宿主内存中拼接完整结果。SJTUClaw
只保留每个输出流的有界头部及用于解析 cwd 标记的少量尾部；Tool 返回最多
64 KiB。stdout 与 stderr 合计超过 8 MiB 时会终止 guest 进程并返回明确错误，
避免无限输出同时压垮 SDK 队列或宿主进程。输出按字节收集，展示时使用 UTF-8
容错解码，因此任意二进制输出不会触发 SDK 的文本解码异常。

## 路径与挂载边界

- guest 工作区固定为 `/workspace`。
- 相对结构化路径相对于 `/workspace` 解析。
- `../`、`/etc/...`、Windows 盘符路径和 UNC 路径不会被结构化文件工具接受。
- Shell 是 microVM 内的完整 Linux 环境，可以访问 guest `/tmp`、`/etc` 等路径；
  这些路径不是宿主同名路径。
- 绑定宿主 workspace 时使用 bind mount，并启用 `nosuid`、`nodev`、
  strict stat virtualization 与 private host permissions。
- 未绑定时使用确定性的 session 命名卷；正常退出只停止 microVM，删除 session
  时才删除该卷。
- 文件读取使用流式 API，在达到 Tool 的字节上限后停止读取。覆盖、编辑和附件
  导入先写入目标同目录的随机临时文件，再通过 rename 提交；写入失败时旧文件
  保持不变。

## 故障策略

`off`、`auto` 与 `required` 决定新 session 的默认开关状态；未显式配置时使用
`off`，因此新 session 默认关闭。显式设置 `auto` 会在 SDK 可发现时选择
sandbox，`required` 始终选择 sandbox。除
`required` 外，用户可以使用 `/sandbox on` 与 `/sandbox off` 覆盖当前 session
的默认值；覆盖状态按 session 保存在当前进程内，不会影响其他会话，应用重启后
恢复 `SANDBOX_MODE` 的默认值。某次操作一旦被路由到 sandbox，创建或执行失败
都会直接返回结构化错误，不在同一次调用中重试宿主执行。

停止 microVM 或删除私有卷失败也会向调用方传播。交互式关闭、workspace 变更
和后端切换只有在清理成功后才报告成功；应用退出阶段无法再重试的清理错误会
写入日志。

当前 microVM 覆盖 SJTUClaw 原生 ToolRegistry。Pi 与 Claude Code 拥有自己的
进程和原生工具循环，尚不能仅靠宿主工具桥获得同等隔离，因此
`required` 模式拒绝切换这两个后端。后续要覆盖它们，应把 agent 进程本身及其
RPC/MCP 通道移入 microVM，而不是只修改启动 cwd。

## 测试边界

单元测试使用同步内存后端验证：

- 无 workspace 自动创建私有卷；
- 文件、Shell 和导出共享同一 session 文件系统；
- workspace 绑定变化会停止旧 microVM 并按新挂载重建；
- 结构化路径不能逃出 `/workspace`；
- `required` 在 SDK 缺失或 UNLIMITED 下 fail-closed；
- session 删除会停止 microVM 并移除私有卷。
- Shell/文件读取有界、非 UTF-8 输出可容错展示；
- 覆盖与附件导入原子提交，失败不破坏旧文件；
- 停止或 volume 删除失败会被报告且保留可重试状态。

真实 microVM 冒烟测试还依赖宿主已安装 microsandbox wheel、启用虚拟化并可取得
所配置 OCI 镜像，因此不应混入不具备这些条件的默认单元测试。
