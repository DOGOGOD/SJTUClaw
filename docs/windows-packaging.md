# Windows 安装包构建

本项目的 Windows 桌面版采用：

- `pywebview`：桌面窗口壳，加载本机 Gateway 托管的 WebUI。
- `PyInstaller`：把 Python 后端、桌面壳和静态资源打包为 `SJTUClaw.exe`。
- `Inno Setup`：生成常见 Windows 安装向导，支持开始菜单和桌面快捷方式。

## 构建环境

- Python 3.11+
- Node.js 18+
- Inno Setup 7（仅生成安装包时需要；脚本也兼容 Inno Setup 6）

## 一键构建

```powershell
.\packaging\windows\build.ps1
```

脚本会安装/校验依赖、构建 Web UI、检查 Tkinter、运行 PyInstaller，并在可用时
调用 Inno Setup。修改 Python、前端、Prompt、Skill 或内置宠物资源后，都需要
重新执行构建；`dist/` 中已有产物不会自动更新。

如果只想生成 PyInstaller 目录版，不生成安装向导：

```powershell
.\packaging\windows\build.ps1 -SkipInstaller
```

## 输出位置

- 桌面应用目录版：`dist\SJTUClaw\SJTUClaw.exe`
- 安装向导：`dist\installer\SJTUClaw-Setup-0.1.0.exe`

## 运行时数据

开发环境仍使用项目内的 `data/`。打包后的安装版会把可写数据放到：

```text
%USERPROFILE%\.sjtuclaw\data
```

其中包括会话、记忆、运行时设置、定时任务、用户宠物和用户技能。
安装版首次启动时也会把内置 `prompts/` 和 `skills/` 复制到该目录，之后 WebUI 中的提示词和 Skill 管理都会写入用户目录，而不是安装目录。

安装包不会内置外部 Pi/Node 运行时。需要 Pi 后端时，应另外安装可执行的 `pi`，
或配置 `PI_COMMAND` / `PI_CLI_PATH`（必要时再配置 `PI_NODE_PATH`）。
