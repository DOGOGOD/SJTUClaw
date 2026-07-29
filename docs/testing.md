# 测试与开发

## 后端测试

```bash
python -m pytest tests/ -v
```

当前测试套件可收集 606 项测试。测试覆盖 Agent Loop、Session/Memory、上下文压缩、
Workspace 回退、Cron、Skill、Gateway、临时下载、Pi/Claude Code 集成、桌宠、
CLI 配置向导以及安全边界。Pi 和 Claude Code 的大多数集成测试使用模拟子进程，
不要求本机安装外部运行时；文件名含 `real` 的检查可能依赖本机已安装并完成登录的
对应工具，运行条件由测试自身判断。

运行单个测试文件：

```bash
python -m pytest tests/test_core.py -v
```

按功能运行常见回归：

```bash
python -m pytest tests/test_cli_setup.py tests/test_cli_repl_cleanup.py -v
python -m pytest tests/test_compaction.py tests/test_workspace_rollback.py -v
python -m pytest tests/test_pi_integration.py tests/test_pi_real_prompt.py -v
python -m pytest tests/test_claude_code_integration.py tests/test_claude_real_prompt.py -v
python -m pytest tests/test_download_registry.py tests/test_qq_media_and_web_images.py -v
python -m pytest tests/test_security_hardening.py tests/test_commands_hardening.py -v
```

## 前端测试与构建

```bash
cd webui
npm ci
npx vitest run
npm run build
```

构建产物输出到项目根目录的 `web/`，Gateway 会直接提供静态文件。

## 开发建议

- 修改 Agent 行为时，优先从 `claw/agent/loop.py` 和 `claw/context/` 入手。
- 增加工具时，在 `claw/tools/` 中实现并通过统一注册入口注册。
- 修改 Web UI 后同时运行前端单测和构建命令。
- 修改文件上传、图片预览或下载交付时，同时覆盖 Gateway 响应头、下载注册表恢复、
  消息 Markdown 去重和 Web UI 下载控件。
- 修改 Pi 或 Claude Code 后端时，分别验证外部会话续接、停止控制、原生工具权限
  和 SJTUClaw 宿主工具桥接。
- 涉及时区、Cron、审批或文件边界的改动，应补充对应回归测试。
- 修改 `.env.example`、CLI setup 或 Web UI 设置项时，同时核对
  `docs/configuration.md` 与运行时设置的读写优先级。
- 修改宠物资源或导入规则时，检查 `pet.json`、图集尺寸、透明通道和未使用帧，
  并运行 `tests/test_pet.py`、`tests/test_bundled_pet_assets.py`。
