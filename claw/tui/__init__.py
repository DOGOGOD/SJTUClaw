"""SJTUClaw terminal user interface."""

from __future__ import annotations


def main() -> int:
    """Run the full-screen terminal interface."""
    from claw.tui.app import SJTUClawTUI

    SJTUClawTUI().run()
    return 0


__all__ = ["main"]
