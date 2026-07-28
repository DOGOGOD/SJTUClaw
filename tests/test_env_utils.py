from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_env_int_uses_default_for_invalid_and_out_of_range_values(monkeypatch):
    from claw.env_utils import env_int

    monkeypatch.setenv("TEST_INTEGER", "not-a-number")
    assert env_int("TEST_INTEGER", 7, minimum=1) == 7

    monkeypatch.setenv("TEST_INTEGER", "0")
    assert env_int("TEST_INTEGER", 7, minimum=1) == 7

    monkeypatch.setenv("TEST_INTEGER", "42")
    assert env_int("TEST_INTEGER", 7, minimum=1, maximum=100) == 42


def test_env_float_rejects_non_finite_and_out_of_range_values(monkeypatch):
    from claw.env_utils import env_float

    for value in ("nan", "inf", "-inf", "invalid", "-1"):
        monkeypatch.setenv("TEST_FLOAT", value)
        assert env_float("TEST_FLOAT", 2.5, minimum=0.0) == 2.5

    monkeypatch.setenv("TEST_FLOAT", "0.25")
    assert env_float("TEST_FLOAT", 2.5, minimum=0.0, maximum=1.0) == 0.25


def test_core_modules_import_with_malformed_numeric_environment():
    env = os.environ.copy()
    env.update({
        "CLAW_MAX_AGENT_ITERATIONS": "invalid",
        "CLAW_MAX_TOOL_CALLS_PER_TURN": "0",
        "LLM_MAX_RETRIES": "invalid",
        "LLM_RETRY_BASE_DELAY": "nan",
        "LLM_REQUEST_TIMEOUT": "-1",
        "COMPACT_KEEP_RECENT_MESSAGES_MIN": "invalid",
        "COMPACT_MAX_MESSAGE_TOKENS": "0",
        "COMPACT_KEEP_RECENT_TOKENS": "invalid",
    })
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import claw.agent; import claw.llm.client; "
                "import claw.context.compaction; print('ok')"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
