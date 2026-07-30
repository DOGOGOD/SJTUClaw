# Sandbox 基础镜像

`sjtuclaw-sandbox:py3.12-bookworm` 是 SJTUClaw 推荐的 microsandbox 镜像。它基于 Python 3.12 Debian Bookworm，并预装常用科学计算、网络、Office 和 PDF 库。

项目专属依赖不放进镜像。SJTUClaw 会让 `python` 和 `pip` 使用 microVM 本地运行环境，并把新增包持久化到 `/workspace/.venv`。

## 预装库

- NumPy、Pandas、SciPy
- Matplotlib、Seaborn、scikit-learn、Pillow
- Requests、HTTPX、Beautiful Soup、lxml、chardet
- OpenPyXL、PyYAML、pypdf、python-docx、ReportLab

准确版本见 [requirements-common.txt](requirements-common.txt)。

## Windows 构建与导入

需要：

- Docker Desktop 已启动
- microsandbox 已安装，`msb.exe` 可用
- Windows Hypervisor Platform 已启用

在项目根目录运行：

```powershell
.\packaging\sandbox\Build-SandboxImage.ps1
```

显式指定 `msb.exe`：

```powershell
.\packaging\sandbox\Build-SandboxImage.ps1 `
  -MsbPath D:\tools\Python\Scripts\msb.exe
```

自定义标签或 PyPI 源：

```powershell
.\packaging\sandbox\Build-SandboxImage.ps1 `
  -Tag sjtuclaw-sandbox:custom `
  -PipIndexUrl https://pypi.org/simple
```

脚本执行 Docker 构建、镜像导出、`msb load`，并等待 microsandbox 缓存确认导入完成。

检查结果：

```powershell
msb image list --format json
```

## 配置与使用

```env
SANDBOX_MODE=off
SANDBOX_IMAGE=sjtuclaw-sandbox:py3.12-bookworm
SANDBOX_PROJECT_VENV=true
SANDBOX_STAT_VIRTUALIZATION=auto
```

重启 SJTUClaw 后，在目标 Session 中运行：

```text
/sandbox on
/sandbox status
```

microVM 在第一次文件或 Shell 工具调用时创建。`/sandbox off` 会停止当前运行实例，但不会删除私有 Workspace Volume 或 `/workspace/.venv` 中的项目依赖。

项目依赖可以直接安装：

```bash
python -m pip install flask
```

不需要手动激活 venv。

## 更新镜像

修改 `requirements-common.txt` 后重新运行构建脚本。Dockerfile 会执行 `pip check` 和关键库导入检查。

已运行的 Session 需要先 `/sandbox off`，再重新开启，才会使用新镜像。

真实环境验证：

```powershell
python tests\sandbox_smoke.py `
  --image sjtuclaw-sandbox:py3.12-bookworm `
  --verify-common-libs
```

该测试实际创建 microVM，并验证通用库、项目 wheel、console script、依赖恢复、文件操作和资源清理。
