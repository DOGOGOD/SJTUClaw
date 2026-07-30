# Code Wiki Log

## 2026-07-30: Move the overview architecture to README

**Updated pages**:

- `README.md`
- `docs/CODE_WIKI.md`

**Change**:

- 将总体架构从 Code Wiki 迁移到 README；图示放在快速开始之后，并突出原生 Agent Loop 的模型与工具迭代过程。

## 2026-07-30: Ingest current SJTUClaw codebase

**Source**: `claw/`, `webui/`, `packaging/`, `pyproject.toml`, `.env.example`

**New pages**:

- `docs/CODE_WIKI.md`
- `docs/code-wiki/concepts/agent-runtime.md`
- `docs/code-wiki/concepts/session-context.md`
- `docs/code-wiki/concepts/tool-system.md`
- `docs/code-wiki/concepts/memory-skill-scheduler.md`
- `docs/code-wiki/concepts/gateway-clients.md`
- `docs/code-wiki/concepts/external-backends.md`
- `docs/code-wiki/patterns/security-boundaries.md`
- `docs/code-wiki/patterns/persistence-layout.md`
- `docs/code-wiki/products/windows-distribution.md`

**Updated pages**:

- `README.md`
- `docs/configuration.md`
- `docs/data-directory-guide.md`
- `docs/sandbox-architecture.md`
- `docs/testing.md`
- `docs/windows-packaging.md`
- `packaging/sandbox/README.md`
- `中期报告.md`
- `script.md`

**New cross-references**:

- Agent Runtime ↔ Session and Context ↔ Tool System
- Tool System ↔ Security Boundaries ↔ External Backends
- Memory, Skill and Scheduler ↔ Session and Context ↔ Persistence Layout
- Gateway and Clients ↔ Windows Distribution ↔ Persistence Layout

## 2026-07-30: Ingest completed TUI implementation

**Source**: `claw/tui/`, `claw/cli/commands.py`, `claw/gateway/server.py`, `tests/test_tui.py`

**New pages**:

- `docs/tui.md`
- `docs/code-wiki/products/terminal-ui.md`

**Updated pages**:

- `README.md`
- `docs/CODE_WIKI.md`
- `docs/testing.md`
- `中期报告.md`
- `docs/code-wiki/concepts/gateway-clients.md`
- `docs/code-wiki/concepts/agent-runtime.md`
- `docs/code-wiki/concepts/memory-skill-scheduler.md`
- `docs/code-wiki/patterns/security-boundaries.md`

**New cross-references**:

- Terminal UI ↔ Gateway and Clients ↔ Agent Runtime
- Terminal UI ↔ Memory, Skill and Scheduler
- Terminal UI ↔ Security Boundaries ↔ Persistence Layout
