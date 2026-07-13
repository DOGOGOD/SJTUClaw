"""Base channel interface for messaging platforms.

Simplified base channel, designed for SJTUClaw's
single-callback architecture (no message bus needed).

Each channel (QQ, etc.) inherits from this class and implements the
platform-specific connection and send logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class OutboundMessage:
    """A message to be delivered through a channel."""

    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


MessageHandler = Callable[
    [str, str, str, list[str] | None, dict[str, Any] | None],
    Awaitable[str | None],
]
"""Async callback signature for inbound messages.

Args:
    sender_id: Platform-specific sender identifier.
    chat_id: Conversation / channel identifier.
    content: Message text.
    media: List of local file paths (attachments).
    metadata: Extra routing context (message_id, etc.).

Returns:
    The assistant's reply text, or ``None`` if no reply should be sent.
"""


# ---------------------------------------------------------------------------
# BaseChannel
# ---------------------------------------------------------------------------


class BaseChannel(ABC):
    """Abstract base for chat platform integrations.

    Subclasses must implement:
    - ``start()`` — connect to the platform and listen for messages.
    - ``stop()`` — disconnect and clean up resources.
    - ``send(msg)`` — deliver an outbound message to the platform.
    """

    name: str = "base"
    display_name: str = "Base"

    def __init__(self, config: Any):
        """Initialise the channel.

        Args:
            config: Channel-specific configuration (Pydantic model or dict).
        """
        self.config = config
        self._running = False
        self._on_message: MessageHandler | None = None

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register the callback that processes inbound messages.

        This is called by the gateway to wire the channel into the agent
        loop.  When a message arrives from the platform, the channel
        should call ``_handle_message()``, which delegates to this
        callback.
        """
        self._on_message = handler

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Forward an inbound message to the registered handler.

        Subclasses call this from their platform-specific receive
        callbacks.

        Returns:
            The reply text from the agent, or ``None``.
        """
        if self._on_message is None:
            return None
        return await self._on_message(sender_id, chat_id, content, media, metadata)

    # ------------------------------------------------------------------
    # Permission check
    # ------------------------------------------------------------------

    def is_allowed(self, sender_id: str) -> bool:
        """Check whether *sender_id* is allowed to interact with the agent.

        Resolution order:
        1. ``"*"`` in allow_from → allow all
        2. Exact match in allow_from list
        3. Otherwise → deny (but the channel may still send a pairing
           code in DMs)
        """
        cfg = self.config
        if isinstance(cfg, dict):
            allow_list: list[str] = (
                cfg.get("allow_from") or cfg.get("allowFrom") or []
            )
        else:
            allow_list = getattr(cfg, "allow_from", None) or []
        if "*" in allow_list or str(sender_id) in allow_list:
            return True
        return False

    # ------------------------------------------------------------------
    # Lifecycle (abstract)
    # ------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Start the channel and begin listening for messages.

        This must be a long-running coroutine that:
        1. Connects to the chat platform.
        2. Listens for incoming messages.
        3. Forwards each message via ``_handle_message()``.
        4. Returns only when the channel is stopped.

        If this coroutine returns, the channel is considered dead.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and release all resources."""
        ...

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Deliver an outbound message to the platform.

        Args:
            msg: The message to send (content, chat_id, media, metadata).
        """
        ...

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the channel is currently active."""
        return self._running

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Return default configuration for this channel type.

        Override in subclasses to auto-populate config stubs.
        """
        return {"enabled": False}
