"""Automatic session title generation via LLM.

When a user sends the first message in a new session, the LLM is asked
to produce a concise summary that captures the core topic.  The result
becomes the session's display title.

Shared by both the Gateway (HTTP) and the CLI (REPL) so that titles
are consistent across entry points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claw.llm.client import LLMClient
    from claw.session.store import SessionStore

logger = logging.getLogger(__name__)

# -- Prompt -----------------------------------------------------------------

_TITLE_SYSTEM_PROMPT = (
    "你是一个对话标题生成器。根据用户的第一条消息，生成一个简洁的标题。\n"
    "规则：\n"
    "- 标题不超过15个字\n"
    "- 捕捉用户的核心意图或问题\n"
    "- 只返回标题文本，不要加引号、标点或任何解释\n"
    "- 使用中文"
)

# Minimum user-message length worth titling (shorter messages keep default title)
_MIN_MESSAGE_LEN = 2
# Truncate very long first messages to avoid burning tokens
_MAX_PROMPT_CHARS = 500
# Maximum allowed title length (after stripping quotes)
_MAX_TITLE_LEN = 30
# Quote characters stripped from both ends of the LLM output
_QUOTE_CHARS = '\'"“”‘’「」『』《》〈〉'


def generate_session_title(first_message: str, llm_client: "LLMClient") -> str | None:
    """Ask the LLM to summarise *first_message* into a session title.

    Returns the title string, or ``None`` if:
    - the message is too short
    - the LLM call fails
    - the result is empty or too long
    """
    text = first_message.strip()
    if not text or len(text) < _MIN_MESSAGE_LEN:
        return None

    prompt = text[:_MAX_PROMPT_CHARS]

    try:
        raw = llm_client.chat([
            {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
    except Exception as exc:
        logger.warning("自动标题 LLM 调用失败: %s", exc)
        return None

    title = raw.strip().strip(_QUOTE_CHARS).strip()
    if not title or len(title) > _MAX_TITLE_LEN:
        return None
    return title


def auto_title_if_first_turn(
    session_id: str,
    messages: list[dict],
    session_store: "SessionStore",
    llm_client: "LLMClient",
) -> str | None:
    """Generate and persist a title when *session_id* has exactly one user message.

    Returns the new title if one was generated, otherwise ``None``.

    Skips when:
    - The session does not exist.
    - There isn't exactly one user message (first-turn only).
    - The user has already manually renamed the session
      (``metadata.title_user_edited``).
    """
    try:
        session = session_store.get(session_id)
    except Exception:
        return None

    # Respect user-edited titles
    if session.metadata.get("title_user_edited"):
        return None

    user_messages = [m for m in messages if m.get("role") == "user"]
    if len(user_messages) != 1:
        return None

    title = generate_session_title(user_messages[0].get("content", ""), llm_client)
    if title is None:
        return None

    try:
        # Use user_edited=False so the auto-generated title can be
        # overwritten if the user sends another message and the system
        # decides to regenerate the title.
        session_store.rename(session_id, title, user_edited=False)
    except Exception as exc:
        logger.warning("自动标题保存失败: %s", exc)
        return None

    logger.info("自动标题: %s → %s", session_id, title)
    return title
