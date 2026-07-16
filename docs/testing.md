# 测试与开发

## 后端测试

```bash
python -m pytest tests/ -v
```

运行单个测试文件：

```bash
python -m pytest tests/test_core.py -v
```

## 前端测试与构建

```bash
cd webui
npm install
npx vitest run
npm run build
```

构建产物输出到项目根目录的 `web/`，Gateway 会直接提供静态文件。

## 开发建议

- 修改 Agent 行为时，优先从 `claw/agent/loop.py` 和 `claw/context/` 入手。
- 增加工具时，在 `claw/tools/` 中实现并通过统一注册入口注册。
- 修改 Web UI 后同时运行前端单测和构建命令。
- 涉及时区、Cron、审批或文件边界的改动，应补充对应回归测试。
