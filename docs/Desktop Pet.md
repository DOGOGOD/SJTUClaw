# SJTUClaw 桌面宠物

桌面宠物是独立于 WebUI 的透明置顶窗口。Gateway 启动时默认加载内置的“月薪喵”，并将 Agent 的任务、工具调用、完成、失败和审批等待状态映射为 Codex 兼容 spritesheet 的动画行。

## 交互

- 按住宠物拖动：播放向左/向右移动动画，并保存屏幕位置。
- 鼠标悬停宠物：播放跳跃动画；移开后恢复当前任务动画。
- 任务进行中：气泡显示当前任务和阶段。
- 进入空闲状态：自动隐藏整个文字气泡，仅保留宠物动画。
- 等待命令审批：播放 waiting 动画；右键宠物可批准或拒绝当前命令。
- 右键“关闭宠物”：关闭窗口并持久化关闭状态。
- v2 宠物空闲时会使用第 9、10 行的 16 方位动画看向鼠标。
- 默认宠物高度为 121 逻辑像素；普通状态动画播放三轮后进入 6 倍慢速待机，与 Codex 桌面宠物节奏一致。

## 后端接口

- `GET /pet/settings`、`PUT /pet/settings`：读取/更新启用状态、选中宠物和开机启动偏好。
- `GET /pet/pets`：列出内置与用户宠物。
- `POST /pet/pets`：multipart 上传 PNG/WebP atlas，并提供 `petId`、`displayName`、`description`、`spriteVersionNumber`。
- `DELETE /pet/pets/{id}`：删除用户宠物；内置宠物不可删除。
- `POST /pet/open`、`POST /pet/close`：开启/关闭桌面宠物。
- `GET /pet/state`：宠物运行时的任务与审批投影。

## WebUI 与命令行

WebUI 的“设置 > 桌面宠物”支持开启、关闭、选择、添加和删除宠物，也可以设置是否随 Gateway 启动。聊天输入框和 CLI REPL 均支持以下命令：

- `/pet` 或 `/pet status`：查看宠物状态；WebUI 中会同时打开宠物设置。
- `/pet list`：列出可用宠物。
- `/pet open`、`/pet close`：开启或关闭宠物。
- `/pet select <petId>`：选择宠物角色。
- `/pet autostart on`、`/pet autostart off`：设置是否随 Gateway 启动。

自定义资源必须使用 192×208 单元格：v1 为 1536×1872（8×9），v2 为 1536×2288（8×11）。

## 独立启动

```powershell
python -m claw.pet --gateway-url http://127.0.0.1:8000
```

该功能依赖 Pillow；项目依赖安装完成后即可运行，不需要 Node/Electron。
Windows 运行时启用高 DPI 感知，spritesheet 会从原始单元格一次缩放到最终物理尺寸，避免系统二次放大造成模糊。
