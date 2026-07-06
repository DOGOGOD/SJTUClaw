"""claw entry point.

Usage:
    python -m claw.main
"""

from __future__ import annotations

import sys

from claw.cli.repl import run_repl
from claw.config import MEMORY_FILE, SESSIONS_DIR, ConfigError, load_config
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient
from claw.memory.store import MemoryStore
from claw.prompts import PromptLoadError, load_soul, load_system_prompt
from claw.session.store import SessionStore
from claw.tools.base import ToolRegistry
from claw.tools.readonly import register_all_readonly


def _force_utf8_streams() -> None:
    """Force stdin/stdout to UTF-8.

    On some Windows setups the console's active code page (e.g. GBK)
    is used instead of UTF-8, which can corrupt non-ASCII input/output
    (including piped input). This keeps CJK text working reliably.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_streams()

    try:
        config = load_config()
        system_prompt = load_system_prompt()
        soul = load_soul()
    except (ConfigError, PromptLoadError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 1

    client = LLMClient(config)
    session_store = SessionStore(SESSIONS_DIR)
    memory_store = MemoryStore(MEMORY_FILE)
    context_builder = ContextBuilder(system_prompt, soul, memory_store)

    # -- Tool registry (Step 5: read-only tools) ----------------------------
    tool_registry = ToolRegistry()
    register_all_readonly(tool_registry)

    run_repl(client, session_store, memory_store, context_builder, tool_registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
