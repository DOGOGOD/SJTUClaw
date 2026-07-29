# Windows 安装包构建

本项目的 Windows 桌面版采用：

- `pywebview`：桌面窗口壳，加载本机 Gateway 托管的 WebUI。
- `PyInstaller`：把 Python 后端、桌面壳和静态资源打包为 `SJTUClaw.exe`。
- `Inno Setup`：生成常见 Windows 安装向导，支持开始菜单和桌面快捷方式。

## 构建环境

- Python 3.11+
- Node.js 18+
- Inno Setup 7（仅生成安装包时需要；脚本也兼容 Inno Setup 6）
- 构建带 sandbox 支持的安装包时，构建 Python 环境需安装
  `microsandbox` wheel，并包含 `msb.exe`、`libkrunfw.dll` 和原生扩展

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
- 安装向导：`dist\installer\SJTUClaw-Setup-0.5.0.exe`

## 运行时数据

开发环境仍使用项目内的 `data/`。打包后的安装版会把可写数据放到：

```text
%USERPROFILE%\.sjtuclaw\data
```

其中包括会话、记忆、运行时设置、定时任务、下载注册表、用户宠物和用户技能。
安装版首次启动时也会把内置 `prompts/` 和 `skills/` 复制到该目录，之后 WebUI 中的提示词和 Skill 管理都会写入用户目录，而不是安装目录。

下载注册表位于 `%USERPROFILE%\.sjtuclaw\data\downloads\registry.json`。升级或重启
不会按时间主动删除入口，但入口引用的 workspace 原文件必须继续存在，且注册表最多
保留 1000 条。卸载程序不会主动删除 `.sjtuclaw` 用户目录。

安装包不会内置外部 Pi/Node 运行时。需要 Pi 后端时，应另外安装可执行的 `pi`，
或配置 `PI_COMMAND` / `PI_CLI_PATH`（必要时再配置 `PI_NODE_PATH`）。
安装包同样不会内置 Claude Code；安装并登录系统 Claude Code 后，SJTUClaw 会从
`PATH`、`%USERPROFILE%\.local\bin\claude.exe` 和常见 npm 目录自动检索。

## Sandbox 打包与镜像

PyInstaller 规格会收集已安装 microsandbox wheel 中的 Python SDK、`msb.exe`、
`libkrunfw.dll` 和原生扩展；缺少关键运行文件时构建会直接失败，避免生成看似可用
但无法启动 microVM 的安装包。目标机器仍需启用 Windows Hypervisor Platform。

`sjtuclaw-sandbox:py3.12-bookworm` 不会打进安装程序。它是约数百 MiB 的独立 OCI
镜像，并由 microsandbox 自己的缓存管理。开发机或目标机首次使用前，应按照
[Sandbox 基础镜像](../packaging/sandbox/README.md) 构建并执行 `msb load`，或者
由发布流程提供已经审核的镜像归档和导入步骤。

镜像和私有 workspace 卷通常位于 `%USERPROFILE%\.microsandbox` 对应的
`MSB_HOME`，不属于 `%USERPROFILE%\.sjtuclaw\data`。覆盖升级 SJTUClaw 不会删除
它们；卸载应用时也不应自动删除用户的 sandbox workspace。

安装版修改 `SANDBOX_IMAGE`、`SANDBOX_MODE` 等设置后需要重启桌面应用。新 session
默认 sandbox 关闭，可在会话中使用 `/sandbox on` 开启；Web UI 会显示 session
级 Sandbox 图标。显式 Sandbox 与 AUTO 状态保存在 Session 中，桌面应用重启后
保持；UNLIMITED 状态重启后关闭。
