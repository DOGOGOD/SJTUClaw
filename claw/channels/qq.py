"""QQ Bot channel using the Official QQ Bot API v2 (direct WebSocket).

Follows ``gateway/platforms/qqbot/adapter.py``.
Connects to the QQ Bot WebSocket Gateway for inbound events and uses the
REST API (``api.sgroup.qq.com``) for outbound messages and media uploads.

Usage — two-step setup:

1. Scan QR to get credentials (one-time)::

    python -m claw.channels.qq_onboard

2. Add to .env and start gateway::

    QQ_ENABLED=true
    QQ_APP_ID=...
    QQ_CLIENT_SECRET=...

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from claw.channels.base import BaseChannel, OutboundMessage
from claw.channels.qq_constants import (
    API_BASE,
    CONNECT_TIMEOUT_SECONDS,
    DEDUP_MAX_SIZE,
    DEDUP_WINDOW_SECONDS,
    GATEWAY_URL_PATH,
    MAX_MESSAGE_LENGTH,
    MAX_QUICK_DISCONNECT_COUNT,
    MEDIA_TYPE_FILE,
    MEDIA_TYPE_IMAGE,
    MAX_RECONNECT_ATTEMPTS,
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_MEDIA,
    MSG_TYPE_TEXT,
    QUICK_DISCONNECT_THRESHOLD,
    RATE_LIMIT_DELAY,
    RECONNECT_BACKOFF,
    TOKEN_URL,
)
from claw.channels.qq_interactions import (
    QQInteraction,
    build_approval_keyboard,
    parse_interaction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class QQConfig:
    """QQ Bot channel configuration."""

    def __init__(
        self,
        enabled: bool = False,
        app_id: str = "",
        client_secret: str = "",
        allow_from: list[str] | None = None,
        markdown_support: bool = True,
        ack_message: str = "",
    ):
        self.enabled = enabled
        self.app_id = app_id
        self.client_secret = client_secret
        self.allow_from: list[str] = allow_from or []
        self.markdown_support = markdown_support
        self.ack_message = ack_message

    @classmethod
    def from_env(cls) -> QQConfig:
        """Load QQ configuration from environment variables."""
        enabled = os.getenv("QQ_ENABLED", "false").strip().lower() in ("true", "1", "yes")
        app_id = os.getenv("QQ_APP_ID", "").strip()
        client_secret = os.getenv("QQ_CLIENT_SECRET", "").strip()
        allow_from_raw = os.getenv("QQ_ALLOW_FROM", "").strip()
        allow_from = [u.strip() for u in allow_from_raw.split(",") if u.strip()] if allow_from_raw else []
        markdown_support = os.getenv("QQ_MSG_FORMAT", "markdown").strip() == "markdown"
        ack_message = os.getenv("QQ_ACK_MESSAGE", "").strip()

        return cls(
            enabled=enabled,
            app_id=app_id,
            client_secret=client_secret,
            allow_from=allow_from,
            markdown_support=markdown_support,
            ack_message=ack_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "app_id": self.app_id,
            "client_secret": "***",
            "allow_from": self.allow_from,
            "markdown_support": self.markdown_support,
        }


# ---------------------------------------------------------------------------
# QQ Bot WebSocket close-code error
# ---------------------------------------------------------------------------


class QQCloseError(Exception):
    """Raised when QQ WebSocket closes with a specific code."""

    def __init__(self, code, reason=""):
        self.code = int(code) if code else None
        self.reason = str(reason) if reason else ""
        super().__init__(f"WebSocket closed (code={self.code}, reason={self.reason})")


# ---------------------------------------------------------------------------
# QQChannel
# ---------------------------------------------------------------------------


class QQChannel(BaseChannel):
    """QQ Bot adapter backed by the official QQ Bot WebSocket Gateway + REST API."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().to_dict()

    def __init__(self, config: QQConfig | dict[str, Any]):
        if isinstance(config, dict):
            config = QQConfig(**config)
        super().__init__(config)
        self.config: QQConfig = config
        self._log_tag = f"QQBot:{self.config.app_id[:8] if self.config.app_id else 'unknown'}"

        # Connection state
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_session: aiohttp.ClientSession | None = None   # fresh per-reconnect
        self._http_client: Any = None  # httpx.AsyncClient for REST API (persistent)
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_interval: float = 30.0
        self._session_id: str | None = None
        self._last_seq: int | None = None

        # Token cache
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        # Dedup
        self._seen_messages: dict[str, float] = {}
        self._last_msg_id: dict[str, str] = {}
        self._interaction_handler: Callable[[QQInteraction], Awaitable[None]] | None = None
        self._pending_media: dict[str, list[str]] = {}

    def set_interaction_handler(
        self, handler: Callable[[QQInteraction], Awaitable[None]]
    ) -> None:
        self._interaction_handler = handler

    def queue_outbound_media(self, chat_id: str, paths: list[str]) -> None:
        if paths:
            self._pending_media.setdefault(chat_id, []).extend(paths)

    def _take_outbound_media(self, chat_id: str) -> list[str]:
        return self._pending_media.pop(chat_id, [])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the QQ bot."""
        if not self.config.app_id or not self.config.client_secret:
            logger.error("[%s] QQ_APP_ID and QQ_CLIENT_SECRET not configured", self._log_tag)
            return

        self._running = True

        try:
            await self._listen_loop()
        finally:
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None

    async def stop(self) -> None:
        """Stop the QQ bot."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        # Clean up aiohttp session (separate from httpx client)
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
        self._ws_session = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("[%s] Stopped", self._log_tag)

    # ------------------------------------------------------------------
    # Listen loop — persistent across reconnects
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Read WebSocket events and reconnect on errors.

        This is the persistent outer loop — it survives reconnections
        so the heartbeat task stays alive across transient drops.
        """
        backoff_idx = 0
        connect_time = 0.0
        quick_disconnect_count = 0

        while self._running:
            try:
                connect_time = time.monotonic()
                await self._connect_and_listen()
                backoff_idx = 0
                quick_disconnect_count = 0
            except asyncio.CancelledError:
                return
            except QQCloseError as exc:
                if not self._running:
                    return
                code = exc.code
                logger.warning("[%s] WebSocket closed: code=%s reason=%s",
                               self._log_tag, code, exc.reason)

                # Quick disconnect detection
                duration = time.monotonic() - connect_time
                if duration < QUICK_DISCONNECT_THRESHOLD and connect_time > 0:
                    quick_disconnect_count += 1
                    if quick_disconnect_count >= MAX_QUICK_DISCONNECT_COUNT:
                        logger.error("[%s] Too many quick disconnects — "
                                     "check AppID/Secret on QQ Open Platform",
                                     self._log_tag)
                        return

                # Fatal codes — stop reconnecting
                if code in {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}:
                    logger.error("[%s] Fatal close code %s — stopping", self._log_tag, code)
                    return

                # Rate limited
                if code == 4008:
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    backoff_idx = 0
                    continue

                # Token invalid — refresh and retry
                if code == 4004:
                    self._access_token = None
                    self._token_expires_at = 0.0

                # Session invalid — clear for re-identify
                # Note: 4009 (timeout) is resumable per QQ protocol, NOT cleared.
                if code in {4006, 4007, 4900, 4901, 4902, 4903, 4904, 4905,
                            4906, 4907, 4908, 4909, 4910, 4911, 4912, 4913}:
                    self._session_id = None
                    self._last_seq = None

                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        logger.error("[%s] Max reconnect attempts (QQCloseError)", self._log_tag)
                        return

            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self._log_tag, exc)

                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached", self._log_tag)
                    return

                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1

    async def _reconnect(self, backoff_idx: int) -> bool:
        """Attempt to reconnect. Returns True on success."""
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        logger.info("[%s] Reconnecting in %ds (attempt %d)...",
                     self._log_tag, delay, backoff_idx + 1)
        await asyncio.sleep(delay)

        self._heartbeat_interval = 30.0  # reset until Hello

        # Close old WebSocket and its session (fresh session = no stale state)
        await self._close_ws()
        try:
            await self._ensure_token()
            gateway_url = await self._get_gateway_url()
            await self._open_ws(gateway_url)
            logger.info("[%s] Reconnected", self._log_tag)
            return True
        except Exception as exc:
            logger.warning("[%s] Reconnect failed: %s", self._log_tag, exc)
            return False

    async def _close_ws(self) -> None:
        """Close WebSocket and its session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
        self._ws_session = None

    async def _connect_and_listen(self) -> None:
        """Authenticate, get gateway URL, open WebSocket, listen."""
        await self._ensure_token()
        gateway_url = await self._get_gateway_url()
        await self._open_ws(gateway_url)

        # Start persistent tasks (survive across reconnects in _listen_loop)
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = asyncio.create_task(self._read_events())
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("[%s] Connected", self._log_tag)

        # Wait for the listen task to complete (block until WS closes)
        try:
            await self._listen_task
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        async with self._token_lock:
            if self._access_token and time.time() < self._token_expires_at - 60:
                return self._access_token

            if not self._http_client:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

            resp = await self._http_client.post(
                TOKEN_URL,
                json={"appId": self.config.app_id, "clientSecret": self.config.client_secret},
            )
            resp.raise_for_status()
            data = resp.json()

            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"Token response missing access_token: {data}")

            expires_in = int(data.get("expires_in", 7200))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info("[%s] Token refreshed, expires in %ds", self._log_tag, expires_in)
            return self._access_token

    async def _get_gateway_url(self) -> str:
        """Fetch the WebSocket gateway URL from the REST API."""
        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "User-Agent": "QQBotAdapter/1.1.0",
        }
        if not self._http_client:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        resp = await self._http_client.get(
            f"{API_BASE}{GATEWAY_URL_PATH}",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        url = data.get("url")
        if not url:
            raise RuntimeError(f"Gateway response missing url: {data}")
        return url

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _open_ws(self, gateway_url: str) -> None:
        """Open a WebSocket connection.

        Creates a **fresh** aiohttp session each time to prevent stale
        connection state from causing spurious disconnects.  Matches
        Matches the ``_open_ws`` pattern.
        """
        # Close old WebSocket + session
        await self._close_ws()

        # Honor WSL proxy env for QQ WebSocket
        self._ws_session = aiohttp.ClientSession(trust_env=True)
        ws_proxy = (
            os.getenv("WSS_PROXY")
            or os.getenv("wss_proxy")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("ALL_PROXY")
            or os.getenv("all_proxy")
        )
        self._ws = await self._ws_session.ws_connect(
            gateway_url,
            headers={"User-Agent": "QQBotAdapter/1.1.0"},
            timeout=CONNECT_TIMEOUT_SECONDS,
            proxy=ws_proxy,
        )
        logger.info("[%s] WebSocket connected to %s", self._log_tag, gateway_url)

    async def _read_events(self) -> None:
        """Read WebSocket frames until connection closes."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    self._dispatch_payload(payload)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QQCloseError(msg.data, msg.extra)
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats."""
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if self._ws and not self._ws.closed:
                    try:
                        await self._ws.send_json({"op": 1, "d": self._last_seq})
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def _send_identify(self) -> None:
        """Send op 2 Identify."""
        token = await self._ensure_token()
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": (1 << 25)  # C2C_GROUP_AT_MESSAGES
                           | (1 << 12)  # DIRECT_MESSAGE
                           | (1 << 26),  # INTERACTION
                "shard": [0, 1],
                "properties": {
                    "$os": "Windows",
                    "$browser": "SJTUClaw",
                    "$device": "SJTUClaw",
                },
            },
        }
        if self._ws and not self._ws.closed:
            await self._ws.send_json(payload)
            logger.info("[%s] Identify sent", self._log_tag)

    async def _send_resume(self) -> None:
        """Send op 6 Resume."""
        token = await self._ensure_token()
        payload = {
            "op": 6,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        }
        if self._ws and not self._ws.closed:
            await self._ws.send_json(payload)
            logger.info("[%s] Resume sent", self._log_tag)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch_payload(self, payload: dict[str, Any]) -> None:
        """Route inbound WebSocket payloads."""
        op = payload.get("op")
        t = payload.get("t")
        s = payload.get("s")
        d = payload.get("d")
        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        # op 10 = Hello
        if op == 10:
            d_data = d if isinstance(d, dict) else {}
            interval_ms = d_data.get("heartbeat_interval", 30000)
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8
            if self._session_id and self._last_seq is not None:
                asyncio.create_task(self._send_resume())
            else:
                asyncio.create_task(self._send_identify())
            return

        # op 0 = Dispatch
        if op == 0 and t:
            if t == "READY":
                self._handle_ready(d)
            elif t in {
                "C2C_MESSAGE_CREATE",
                "GROUP_AT_MESSAGE_CREATE",
                "DIRECT_MESSAGE_CREATE",
            }:
                asyncio.create_task(self._on_ws_event(t, d))
            elif t == "INTERACTION_CREATE":
                asyncio.create_task(self._on_interaction(d))
            return

        # op 11 = Heartbeat ACK
        if op == 11:
            return

        # op 7 = Server reconnect
        if op == 7:
            logger.info("[%s] Server requested reconnect", self._log_tag)
            if self._ws and not self._ws.closed:
                asyncio.create_task(self._ws.close())
            return

        # op 9 = Invalid Session
        if op == 9:
            resumable = bool(d) if d is not None else False
            if not resumable:
                self._session_id = None
                self._last_seq = None
            if self._ws and not self._ws.closed:
                asyncio.create_task(self._ws.close())
            return

    def _handle_ready(self, d: Any) -> None:
        """Store session_id from READY."""
        if isinstance(d, dict):
            self._session_id = d.get("session_id")
            logger.info("[%s] Ready, session_id=%s", self._log_tag, self._session_id)

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------
    # Inbound message handling
    # ------------------------------------------------------------------

    def _is_duplicate(self, msg_id: str) -> bool:
        """Check if a message ID has been seen recently."""
        now = time.time()
        # Prune old entries
        self._seen_messages = {
            k: v for k, v in self._seen_messages.items()
            if now - v < DEDUP_WINDOW_SECONDS
        }
        if msg_id in self._seen_messages:
            return True
        if len(self._seen_messages) >= DEDUP_MAX_SIZE:
            return False
        self._seen_messages[msg_id] = now
        return False

    async def _on_ws_event(self, event_type: str, d: Any) -> None:
        """Process an inbound QQ Bot message event."""
        if not isinstance(d, dict):
            return

        msg_id = str(d.get("id", ""))
        if not msg_id or self._is_duplicate(msg_id):
            return

        content = str(d.get("content", "")).strip()
        author = d.get("author") if isinstance(d.get("author"), dict) else {}

        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c(d, msg_id, content, author)
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            await self._handle_group(d, msg_id, content, author)
        elif event_type == "DIRECT_MESSAGE_CREATE":
            await self._handle_dm(d, msg_id, content, author)

    async def _handle_c2c(
        self, d: dict[str, Any], msg_id: str, content: str, author: dict[str, Any]
    ) -> None:
        """Handle a C2C (private) message."""
        user_openid = str(author.get("user_openid", ""))
        if not user_openid:
            return
        if not self.is_allowed(user_openid):
            return

        self._last_msg_id[user_openid] = msg_id

        logger.info("[%s] C2C from %s: %s", self._log_tag, user_openid, content[:80])

        reply = await self._handle_message(
            sender_id=user_openid,
            chat_id=user_openid,
            content=content,
            metadata={"message_id": msg_id, "chat_type": "c2c"},
        )

        if reply:
            await self.send(
                OutboundMessage(
                    chat_id=user_openid,
                    content=reply,
                    media=self._take_outbound_media(user_openid),
                    metadata={"message_id": msg_id, "chat_type": "c2c"},
                )
            )

    async def _handle_group(
        self, d: dict[str, Any], msg_id: str, content: str, author: dict[str, Any]
    ) -> None:
        """Handle a group @-message."""
        group_openid = str(d.get("group_openid", ""))
        if not group_openid:
            return

        member_openid = str(author.get("member_openid", ""))
        if not member_openid or not self.is_allowed(member_openid):
            logger.warning(
                "[%s] Rejected group message from unauthorised member %s",
                self._log_tag,
                member_openid or "<missing>",
            )
            return

        # Strip @bot mention
        text = content

        self._last_msg_id[group_openid] = msg_id

        logger.info("[%s] Group %s from %s: %s", self._log_tag, group_openid, member_openid, text[:80])

        reply = await self._handle_message(
            sender_id=member_openid,
            chat_id=group_openid,
            content=text,
            metadata={"message_id": msg_id, "chat_type": "group", "member_openid": member_openid},
        )

        if reply:
            await self.send(
                OutboundMessage(
                    chat_id=group_openid,
                    content=reply,
                    media=self._take_outbound_media(group_openid),
                    metadata={"message_id": msg_id, "chat_type": "group"},
                )
            )

    async def _handle_dm(
        self, d: dict[str, Any], msg_id: str, content: str, author: dict[str, Any]
    ) -> None:
        """Handle a guild DM message."""
        guild_id = str(d.get("guild_id", ""))
        author_id = str(author.get("id", ""))
        if not guild_id or not author_id:
            return
        if not self.is_allowed(author_id):
            return

        self._last_msg_id[guild_id] = msg_id

        reply = await self._handle_message(
            sender_id=author_id,
            chat_id=guild_id,
            content=content,
            metadata={"message_id": msg_id, "chat_type": "dm"},
        )

        if reply:
            await self.send(
                OutboundMessage(
                    chat_id=guild_id,
                    content=reply,
                    media=self._take_outbound_media(guild_id),
                    metadata={"message_id": msg_id, "chat_type": "dm"},
                )
            )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """Send text plus any network/local media to a QQ user or group."""
        if not msg.content.strip() and not msg.media:
            return

        if msg.media:
            caption = msg.content
            sent_media = False
            for item in msg.media:
                try:
                    await self._send_media(msg, item, caption if not sent_media else "")
                    sent_media = True
                except Exception as exc:
                    logger.error("[%s] Failed to send media %s: %s", self._log_tag, item, exc)
            if sent_media:
                return

        # Truncate long messages
        text = msg.content
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH - 20] + "\n\n...(消息过长已截断)"

        chat_type = msg.metadata.get("chat_type", "c2c") if msg.metadata else "c2c"
        msg_id = msg.metadata.get("message_id") if msg.metadata else None

        try:
            token = await self._ensure_token()

            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
                "User-Agent": "QQBotAdapter/1.1.0",
            }

            is_group = chat_type == "group"
            use_markdown = self.config.markdown_support

            body: dict[str, Any] = {
                "msg_type": MSG_TYPE_MARKDOWN if use_markdown else MSG_TYPE_TEXT,
                "msg_id": msg_id,
            }
            if use_markdown:
                body["markdown"] = {"content": text}
            else:
                body["content"] = text

            if not self._http_client:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

            if is_group:
                await self._http_client.post(
                    f"{API_BASE}/v2/groups/{msg.chat_id}/messages",
                    headers=headers,
                    json=body,
                )
            else:
                await self._http_client.post(
                    f"{API_BASE}/v2/users/{msg.chat_id}/messages",
                    headers=headers,
                    json=body,
                )

            logger.debug("[%s] Message sent to %s", self._log_tag, msg.chat_id)

        except Exception as exc:
            logger.error("[%s] Failed to send message: %s", self._log_tag, exc)

    async def send_approval(
        self,
        chat_id: str,
        chat_type: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        message_id: str | None = None,
    ) -> None:
        args_text = json.dumps(tool_args, ensure_ascii=False, default=str)
        if len(args_text) > 900:
            args_text = args_text[:900] + "…"
        text = (
            f"⚠️ 需要审批\n工具：{tool_name}\n参数：{args_text}\n\n"
            f"也可回复 /approve {approval_id} 或 /reject {approval_id} [原因]"
        )
        await self._send_text_body(
            chat_id, chat_type, text, message_id, keyboard=build_approval_keyboard(approval_id)
        )

    async def _send_text_body(
        self,
        chat_id: str,
        chat_type: str,
        text: str,
        message_id: str | None,
        keyboard: dict[str, Any] | None = None,
    ) -> None:
        token = await self._ensure_token()
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        use_markdown = self.config.markdown_support
        body: dict[str, Any] = {
            "msg_type": MSG_TYPE_MARKDOWN if use_markdown else MSG_TYPE_TEXT,
            "msg_id": message_id,
        }
        body["markdown" if use_markdown else "content"] = (
            {"content": text} if use_markdown else text
        )
        if keyboard is not None and chat_type in {"c2c", "group"}:
            body["keyboard"] = keyboard
        if not self._http_client:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        target = "groups" if chat_type == "group" else "users"
        response = await self._http_client.post(
            f"{API_BASE}/v2/{target}/{chat_id}/messages", headers=headers, json=body
        )
        response.raise_for_status()

    async def _send_media(self, msg: OutboundMessage, source: str, caption: str) -> None:
        chat_type = msg.metadata.get("chat_type", "c2c") if msg.metadata else "c2c"
        message_id = msg.metadata.get("message_id") if msg.metadata else None
        is_url = source.startswith(("https://", "http://"))
        suffix = Path(source.split("?", 1)[0]).suffix.lower()
        mime_type = mimetypes.guess_type(source)[0] or ""
        file_type = MEDIA_TYPE_IMAGE if mime_type.startswith("image/") or suffix in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"
        } else MEDIA_TYPE_FILE
        body: dict[str, Any] = {"file_type": file_type, "srv_send_msg": False}
        if is_url:
            body["url"] = source
        else:
            path = Path(source).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(source)
            if path.stat().st_size > 9 * 1024 * 1024:
                raise ValueError("QQ 内联文件上传上限为 9 MB")
            raw = await asyncio.to_thread(path.read_bytes)
            body["file_data"] = base64.b64encode(raw).decode("ascii")
            if file_type == MEDIA_TYPE_FILE:
                body["file_name"] = path.name

        token = await self._ensure_token()
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        if not self._http_client:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        target = "groups" if chat_type == "group" else "users"
        upload = await self._http_client.post(
            f"{API_BASE}/v2/{target}/{msg.chat_id}/files", headers=headers, json=body
        )
        upload.raise_for_status()
        upload_data = upload.json()
        file_info = upload_data.get("file_info") or (upload_data.get("data") or {}).get("file_info")
        if not file_info:
            raise RuntimeError("QQ upload response missing file_info")
        message_body: dict[str, Any] = {
            "msg_type": MSG_TYPE_MEDIA,
            "media": {"file_info": file_info},
            "msg_id": message_id,
        }
        if caption:
            message_body["content"] = caption[:MAX_MESSAGE_LENGTH]
        response = await self._http_client.post(
            f"{API_BASE}/v2/{target}/{msg.chat_id}/messages", headers=headers, json=message_body
        )
        response.raise_for_status()
    async def _on_interaction(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        event = parse_interaction(raw)
        if event.interaction_id:
            try:
                token = await self._ensure_token()
                headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
                if not self._http_client:
                    import httpx
                    self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
                response = await self._http_client.put(
                    f"{API_BASE}/interactions/{event.interaction_id}", headers=headers, json={"code": 0}
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning("[%s] Failed to ACK interaction: %s", self._log_tag, exc)
        if self._interaction_handler:
            await self._interaction_handler(event)
