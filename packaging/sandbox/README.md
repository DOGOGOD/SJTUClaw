# SJTUClaw Sandbox 基础镜像

该镜像以 `python:3.12-bookworm` 为基础，预装多数 session 会复用的科学计算、
绘图、网络访问、HTML、Office 和 PDF Python 库。具体版本记录在
`requirements-common.txt`。

默认镜像标签为：

```text
sjtuclaw-sandbox:py3.12-bookworm
```

项目专属依赖不应继续安装到镜像系统 Python。SJTUClaw 会在 Sandbox 启动时创建
microVM-local 运行 venv，让 `python` 与 `pip` 默认使用它，并通过
`--system-site-packages` 读取镜像通用库。项目新增包和 console scripts 会自动
同步到 `/workspace/.venv`，从而跨 microVM 重建持久化。

## 已预装的通用库

当前包含 NumPy、Pandas、SciPy、Matplotlib、Seaborn、scikit-learn、Pillow、
Requests、HTTPX、Beautiful Soup、lxml、chardet、OpenPyXL、PyYAML、pypdf、
python-docx 和 ReportLab。版本以
[`requirements-common.txt`](requirements-common.txt) 为准。

## Windows 构建与导入

准备条件：

- Docker Desktop 已安装并启动；
- microsandbox 已安装，`msb.exe` 可用；
- Windows Hypervisor Platform 已启用。

在项目根目录运行：

```powershell
.\packaging\sandbox\Build-SandboxImage.ps1
```

脚本通常能自动找到 `msb.exe`。找不到时显式指定：

```powershell
.\packaging\sandbox\Build-SandboxImage.ps1 `
  -MsbPath D:\tools\Anaconda\Scripts\msb.exe
```

脚本会依次执行 Docker 构建、`docker save`、`msb load`，并等待 microsandbox
确认标签已经写入镜像缓存。microsandbox 不能直接读取 Docker Desktop 的本地镜像
仓库，所以导入步骤不可省略。

构建后可检查：

```powershell
msb image list --format json
```

输出中应包含 `sjtuclaw-sandbox:py3.12-bookworm`。

构建完成后设置：

```env
SANDBOX_MODE=off
SANDBOX_IMAGE=sjtuclaw-sandbox:py3.12-bookworm
SANDBOX_PROJECT_VENV=true
SANDBOX_STAT_VIRTUALIZATION=auto
```

修改配置后需要重启 CLI、Gateway 或桌面应用。

## 在会话中使用

新 session 默认关闭 sandbox。重启 SJTUClaw 后，在目标会话输入：

```text
/sandbox
/sandbox on
/sandbox status
```

`/sandbox` 显示可用命令，`/sandbox on` 只开启当前 session，Web UI 会在会话上显示
Sandbox 图标。首次文件或 Shell 工具调用才按需启动 microVM。完成后可输入
`/sandbox off` 停止该 session 的 microVM；私有 workspace 卷不会因此删除。
显式 on/off 状态保存在 Session 中，重启 SJTUClaw 后保持。若状态为 on 但镜像、
虚拟化或 microsandbox 运行时不可用，工具会报错并拒绝回退到宿主执行。

通用库可以直接导入：

```python
import matplotlib
import numpy
import pandas
```

项目专属依赖直接正常安装：

```bash
python -m pip install flask
```

无需激活 `/workspace/.venv`。它是 SJTUClaw 管理的持久依赖存储，不是供用户
直接 `source` 的完整虚拟环境。

## 更新通用库

修改 `requirements-common.txt` 后重新运行构建脚本。构建过程会执行 `pip check`
和关键库导入检查；成功后，新启动的 microVM 使用新镜像。已运行的 microVM 应先
通过 `/sandbox off` 停止，再重新开启。

真实环境验证：

```powershell
python tests\sandbox_smoke.py `
  --image sjtuclaw-sandbox:py3.12-bookworm `
  --verify-common-libs
```

该测试会实际创建 microVM，验证通用库、项目 wheel 安装、console script、重启
恢复、文件操作和资源清理。
