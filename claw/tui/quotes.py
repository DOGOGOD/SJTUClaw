"""Curated computer-science quotes for the TUI welcome surface."""

from __future__ import annotations

import random

QUOTES: tuple[tuple[str, str], ...] = (
    ("Programs must be written for people to read.", "Harold Abelson"),
    ("Simplicity is prerequisite for reliability.", "Edsger W. Dijkstra"),
    ("The best way to predict the future is to invent it.", "Alan Kay"),
    ("Talk is cheap. Show me the code.", "Linus Torvalds"),
    ("Make it work, make it right, make it fast.", "Kent Beck"),
    (
        (
            "The purpose of abstraction is not to be vague, "
            "but to create a new semantic level."
        ),
        "Edsger W. Dijkstra",
    ),
    (
        "Any sufficiently advanced technology is indistinguishable from magic.",
        "Arthur C. Clarke",
    ),
    ("First, solve the problem. Then, write the code.", "John Johnson"),
    (
        "Controlling complexity is the essence of computer programming.",
        "Brian Kernighan",
    ),
    (
        "The computer was born to solve problems that did not exist before.",
        "Bill Gates",
    ),
    ("代码写给人看，只是顺便让机器执行。", "Harold Abelson"),
    (
        "计算机科学并不只是关于计算机，就像天文学并不只是关于望远镜。",
        "Edsger W. Dijkstra",
    ),
)


def random_quote() -> tuple[str, str]:
    """Return a random quote and attribution."""
    return random.SystemRandom().choice(QUOTES)
