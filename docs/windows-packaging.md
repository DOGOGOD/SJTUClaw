# Windows 安装包构建

Windows 发布流程分两步：

1. PyInstaller 生成可独立运行的桌面程序目录。
2. Inno Setup 生成标准安装程序。

## 构建环境

- 64 位 Windows
- Python 3.11+，并包含完整 Tcl/Tk
- Node.js 与 npm
- Inno Setup 6 或 7
- 可用的 `microsandbox>=0.6.7,<0.7`

推荐使用独立的 `.venv-build`。

## 一键构建

在项目根目录运行：

```powershell
.\packaging\windows\build.ps1
```

指定 Python：

```powershell
.\packaging\windows\build.ps1 -PythonExe C:\Python311\python.exe
```

只构建 PyInstaller 程序：

```powershell
.\packaging\windows\build.ps1 -SkipInstaller
```

脚本会依次执行：

1. `npm ci` 和 Web UI 生产构建。
2. 安装 `.[build]` 依赖。
3. 检查 Tkinter。
4. 执行 `SJTUClaw.spec`。
5. 查找 `ISCC.exe` 并编译安装程序。

## 输出

```text
dist/SJTUClaw/SJTUClaw.exe
dist/installer/SJTUClaw-Setup-0.5.0.exe
```

安装程序支持中英文向导、自选安装目录、开始菜单、可选桌面快捷方式、覆盖升级和系统卸载入口。

源码环境中可使用统一 CLI 启动桌面窗口：

```powershell
sjtuclaw desktop
```

## 打包内容

`SJTUClaw.spec` 收集：

- Python 运行时、FastAPI、pywebview 和 Uvicorn
- 已构建的 `web/`
- `prompts/`、`skills/` 和内置宠物
- Pi TypeScript 桥接文件
- Sandbox 项目环境同步脚本
- `.env.example`
- microsandbox SDK、原生扩展、`msb.exe` 和 `libkrunfw.dll`

构建时若缺少 microsandbox 原生扩展或关键运行文件会直接失败。Qt 后端被显式排除，桌面界面使用 Windows 原生 pywebview。

## 运行时数据

安装目录视为只读资源目录。可写数据位于：

```text
%USERPROFILE%\.sjtuclaw\
├── .env
└── data/
```

覆盖升级和卸载不会主动删除用户数据。Pi、Claude Code 和 microsandbox 镜像也不随安装包提供或删除：

- Pi 与 Claude Code 需要用户单独安装。
- Sandbox 镜像由 microsandbox 的 `MSB_HOME` 管理。
- 私有 Sandbox Workspace 不位于 `.sjtuclaw\data`。

## 发布前检查

```powershell
python -m pytest tests/ -v
cd webui
npx vitest run
npm run build
```

构建后至少验证：

- 未配置 LLM 时可以打开设置界面。
- 配置后可以创建 Session 并完成一轮对话。
- Web UI 静态资源、图片附件和文件下载正常。
- 桌面宠物可以启动和关闭。
- 安装、覆盖升级和卸载正常，用户数据仍保留。
- 目标机启用 Windows Hypervisor Platform 后可以运行 `msb doctor`。

Sandbox 镜像不打进安装程序。其构建与导入见 [Sandbox 基础镜像](../packaging/sandbox/README.md)。
