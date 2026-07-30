# 测试与开发

## 后端测试

在项目根目录运行：

```powershell
python -m pytest tests/ -v
```

当前测试套件可收集 741 项测试；本轮验证基线为 `737 passed, 4 skipped, 2 subtests passed`。

覆盖范围包括：

- Agent Loop、工具协议、预算、取消和健康监控
- Session、Memory、上下文压缩和并发持久化
- Workspace、回退、审批、AUTO / UNLIMITED
- Gateway、SSE、上传、下载和安全中间件
- Cron、Heartbeat、Reflection 和 Skill 生命周期
- Pi、Claude Code、QQ、桌宠、CLI、TUI 和路径切换
- Sandbox 配置、路由、项目依赖同步和 Windows 运行时

大多数外部 Agent 测试使用模拟子进程，不要求本机安装 Pi 或 Claude Code。文件名含 `real` 的测试可能依赖本机环境，并在条件不满足时跳过。

常用局部测试：

```powershell
python -m pytest tests/test_gateway_fixes.py -v
python -m pytest tests/test_security_hardening.py -v
python -m pytest tests/test_sandbox_integration.py -v
python -m pytest tests/test_pi_integration.py tests/test_claude_code_integration.py -v
python -m pytest tests/test_tui.py -v
```

TUI 测试使用 Textual Pilot 覆盖响应式布局、输入历史、命令补全、Session / Cron 看板、流式事件、审批、停止任务、并发命令保护和运行时清理，不要求真实终端交互。

只检查收集：

```powershell
python -m pytest tests/ --collect-only -q
```

## Sandbox 真实环境测试

普通 pytest 使用替身验证路由和故障逻辑。真实 microVM 需要额外运行：

```powershell
python tests\sandbox_smoke.py `
  --image sjtuclaw-sandbox:py3.12-bookworm `
  --verify-common-libs
```

前置条件：

- Windows Hypervisor Platform 已启用
- microsandbox SDK 和 `msb.exe` 可用
- 镜像已通过 `msb load` 导入

该脚本会实际验证 microVM 创建、通用库、项目 wheel、console script、依赖持久化、重启恢复、文件操作和资源清理。

## 前端测试与构建

```powershell
cd webui
npm install
npx vitest run
npm run build
```

`npm run build` 先执行 TypeScript 检查，再由 Vite 把静态资源写入项目根目录 `web/`。修改 `webui/` 后应同时提交新的已跟踪构建产物。

## 代码检查

```powershell
python -m ruff check claw tests
python -m compileall -q claw
```

## 提交前建议

1. 先运行与改动模块直接相关的测试。
2. 再运行完整 pytest 和前端 Vitest。
3. 修改前端时执行生产构建。
4. 修改 Sandbox、Windows 打包或外部 Agent 桥接时，补做对应真实环境验证。
5. 不提交 `.env`、`data/`、测试缓存、构建中间目录或本机运行时文件。
