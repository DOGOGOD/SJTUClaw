"""Token counting with tiktoken, falling back to character heuristic.

Provides ``count_tokens`` and ``count_tokens_for_messages`` as the
single source of truth for token estimation across the codebase.
"""

from __future__ import annotations

from claw.session.models import Message

# ---------------------------------------------------------------------------
# Encoder selection (lazy, at import time)
# ---------------------------------------------------------------------------

_ENC = None

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:
    pass


def _is_cjk(ch: str) -> bool:
    """Rough CJK unified ideograph range check."""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0xF900 <= cp <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x3000 <= cp <= 0x303F  # CJK Symbols and Punctuation
        or 0xFF00 <= cp <= 0xFFEF  # Halfwidth and Fullwidth Forms
        or 0x2E80 <= cp <= 0x2FDF  # CJK Radicals Supplement
        or 0x31C0 <= cp <= 0x31EF  # CJK Strokes
    )


def count_tokens(text: str) -> int:
    """Return the estimated token count for *text*.

    Uses tiktoken ``o200k_base`` when available (good approximation for
    GPT-4o / Claude-family models).  Falls back to a conservative
    character-based heuristic that treats CJK as 2 tokens/char and
    everything else as 4 chars/token.
    """
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))

    # Fallback heuristic
    cn = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cn
    return cn * 2 + max(1, other // 4)


def count_tokens_for_messages(messages: list[Message]) -> int:
    """Return the token count for the *content* of *messages*.

    Only message content is counted (role strings are not included),
    matching the semantics of the old ``MAX_CHARS_BEFORE_COMPACTION``
    threshold.
    """
    return sum(count_tokens(m.content) for m in messages)
