from __future__ import annotations

import sys

import pytest

import claw.cli.main as cli_main


def test_bare_sjtuclaw_requires_an_explicit_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["sjtuclaw"])

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main()

    assert exc_info.value.code == 2


def test_chat_subcommand_is_the_explicit_cli_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["sjtuclaw", "chat"])
    monkeypatch.setattr(cli_main, "_cmd_chat", lambda: 17)

    assert cli_main.main() == 17


def test_desktop_subcommand_uses_the_unified_cli_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["sjtuclaw", "desktop"])
    monkeypatch.setattr(cli_main, "_cmd_desktop", lambda: 23)

    assert cli_main.main() == 23
