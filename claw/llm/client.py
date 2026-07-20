"""Minimal LLM API client.

Wraps an OpenAI-compatible chat completions endpoint with two entry
points:

    chat(messages) -> str                 # simple text-only call
    chat_with_tools(messages, tools) -> AgentResponse   # tool-calling call

Both translate low-level failures into clear, user-facing errors.

Reliability features:
- Configurable request timeout (default 120s) prevents indefinite hangs.
- Automatic retry for transient network errors (connection resets,
  timeouts, 429/5xx) with exponential backoff.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Iterable, Mapping, TYPE_CHECKING

import openai
from openai import OpenAI

from claw.config import LLMConfig
from claw.llm.protocol import AgentResponse, ProtocolParseError, parse_agent_response

if TYPE_CHECKING:
    from claw.context.budget import ContextBudget

Message = Mapping[str, Any]

logger = logging.getLogger(__name__)

# Patterns scrubbed from error messages before surfacing to callers.
# Prevents accidental API key / credential leakage in user-facing errors.
_SECRET_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{20,})"),          # OpenAI-style keys
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.]{20,})", re.IGNORECASE),
    re.compile(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,})", re.IGNORECASE),
)


def _scrub_secrets(text: str) -> str:
    """Redact credentials from *text* before logging or returning."""
    if not text:
        return text
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("***REDACTED***", redacted)
    return redacted

# -- Retry configuration (env-overridable) ----------------------------------
_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
"""Number of automatic retries for transient failures (0 = no retry)."""

_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))
"""Base delay in seconds for exponential backoff."""

_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "120"))
"""Per-request timeout in seconds (prevents indefinite hangs)."""


class LLMError(RuntimeError):
    """Base class for all LLM-related errors shown to the user."""


class LLMConnectionError(LLMError):
    """Raised when the LLM API could not be reached (network failure)."""


class LLMResponseStatusError(LLMError):
    """Raised when the LLM API responds with an HTTP error status."""


class LLMResponseFormatError(LLMError):
    """Raised when the LLM API response body cannot be parsed as expected."""


def _is_transient_error(exc: Exception) -> bool:
    """Return True for errors worth retrying (network blips, 429, 5xx)."""
    if isinstance(exc, openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APITimeoutError):
        return True
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0)
        return status >= 500
    return False


class LLMClient:
    """Thin wrapper around an OpenAI-compatible chat completion API."""

    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=_REQUEST_TIMEOUT,
        )

    @property
    def config(self) -> LLMConfig:
        """Return the LLM configuration (read-only access)."""
        return self._config

    # -- simple text call (backward-compatible) -------------------------------

    def chat(
        self,
        messages: Iterable[Message],
        budget: "ContextBudget | None" = None,
    ) -> str:
        """Send `messages` to the LLM and return the assistant's reply text.

        Args:
            messages: an ordered list of `{"role": ..., "content": ...}`
                dicts, following the usual chat completion convention.
            budget: optional ``ContextBudget`` for last-mile overflow check.

        Returns:
            The assistant's reply text.

        Raises:
            LLMConnectionError: if the request could not reach the server
                (DNS failure, connection refused, timeout, etc.).
            LLMResponseStatusError: if the server returned an HTTP error
                status (e.g. invalid API key, invalid model).
            LLMResponseFormatError: if the response body does not contain
                the expected fields.
            LLMError: for any other unexpected failure from the SDK.
        """
        response = self._call_api(list(messages), budget=budget)
        return self._extract_reply_text(response)

    # -- tool-calling call ---------------------------------------------------

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]] | None = None,
        budget: "ContextBudget | None" = None,
    ) -> AgentResponse:
        """Send *messages* with optional tool definitions and return a
        structured ``AgentResponse``.

        If *tool_definitions* are provided and non-empty, they are passed
        as the API ``tools`` parameter so the model can request tool
        calls natively (OpenAI function-calling protocol). The response
        is then parsed via ``parse_agent_response``, which prefers native
        ``tool_calls`` and falls back to JSON-protocol parsing of the
        text content.

        Args:
            messages: the full context (including any previous tool
                results as ``tool``-role messages).
            tool_definitions: OpenAI-format tool definitions from
                ``ToolRegistry.list_definitions()``, or None.
            budget: optional ``ContextBudget`` for last-mile overflow
                check.

        Returns:
            ``AgentResponse`` with either ``final`` text or ``tool_calls``.

        Raises:
            LLMConnectionError / LLMResponseStatusError /
                LLMResponseFormatError / LLMError: on API-level failures.
        """
        response = self._call_api(
            messages, tool_definitions=tool_definitions, budget=budget
        )
        if not response.choices:
            raise LLMResponseFormatError("LLM 响应中没有 choices 字段或 choices 为空")
        choice = response.choices[0]

        # Extract native tool calls if present
        native_tool_calls: list[dict[str, Any]] | None = None
        msg = choice.message
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            native_tool_calls = []
            for tc in msg.tool_calls:
                # Defensive: some providers omit `function` on malformed calls.
                fn = getattr(tc, "function", None)
                if fn is None:
                    logger.warning("跳过缺少 function 字段的 tool_call: id=%s", getattr(tc, "id", "?"))
                    continue
                native_tool_calls.append({
                    "id": getattr(tc, "id", "") or "",
                    "function": {
                        "name": getattr(fn, "name", "") or "",
                        "arguments": getattr(fn, "arguments", "") or "",
                    },
                })
            if not native_tool_calls:
                native_tool_calls = None

        raw_content = getattr(msg, "content", None)
        content_text: str | None = raw_content if isinstance(raw_content, str) else None
        finish_reason: str | None = getattr(choice, "finish_reason", None)

        try:
            parsed = parse_agent_response(content_text, native_tool_calls, finish_reason)
        except ProtocolParseError as exc:
            raise LLMResponseFormatError(
                f"LLM 响应协议解析失败：{exc}\n"
                f"原始内容（前200字符）：{str(content_text or '')[:200]}"
            ) from exc

        # Propagate finish_reason for length-truncation recovery
        parsed.finish_reason = finish_reason
        return parsed

    # -- internal helpers ----------------------------------------------------

    def _call_api(
        self,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]] | None = None,
        budget: "ContextBudget | None" = None,
    ) -> Any:
        """Call the chat completions API and return the raw response.

        If *budget* is provided, a last-mile overflow check is performed
        before the call.  A ``ContextOverflowError`` is raised when the
        context exceeds 105 % of the budget (the model would silently
        truncate).  Between 100 % and 105 % only a warning is printed.
        """
        # -- Last-mile safety check ---------------------------------------
        if budget is not None:
            budget.check_overflow()
            if budget.usage_ratio >= 1.0:
                logger.warning(
                    "上下文 token 数 (%s) 已达预算上限 (%s)，建议执行 /compact 压缩会话。",
                    budget.total_tokens, budget.max_tokens,
                )

        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,  # type: ignore[arg-type]
        }
        if tool_definitions:
            kwargs["tools"] = tool_definitions  # type: ignore[arg-type]
            # Let the model decide when to call tools; don't force it
            # kwargs["tool_choice"] = "auto"

        try:
            return self._call_api_with_retry(kwargs)
        except openai.APIConnectionError as exc:
            raw = _scrub_secrets(str(exc))
            logger.error("LLM APIConnectionError: %s", raw)
            raise LLMConnectionError(
                "无法连接到 LLM 服务，请检查网络连接以及 LLM_BASE_URL 配置是否正确。"
            ) from exc
        except openai.APIStatusError as exc:
            # Log full detail server-side, but only expose status code to user.
            raw_msg = _scrub_secrets(str(getattr(exc, "message", "") or exc))
            logger.error("LLM APIStatusError %s: %s", exc.status_code, raw_msg)
            raise LLMResponseStatusError(
                f"LLM 服务返回了错误状态码 {exc.status_code}。"
                "请检查 LLM_API_KEY、LLM_MODEL、LLM_BASE_URL 是否正确、是否有权限访问该模型。"
            ) from exc
        except openai.OpenAIError as exc:
            raw = _scrub_secrets(str(exc))
            logger.error("LLM OpenAIError: %s", raw)
            raise LLMError("调用 LLM 服务时发生未知错误，请查看日志。") from exc

    def _call_api_with_retry(self, kwargs: dict[str, Any]) -> Any:
        """Call the API with automatic retry for transient failures.

        Retries up to ``_MAX_RETRIES`` times on connection errors,
        timeouts, rate limits, and 5xx server errors using exponential
        backoff. Non-transient errors (auth, validation, 4xx) are raised
        immediately.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                if not _is_transient_error(exc) or attempt >= _MAX_RETRIES:
                    raise
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "LLM 请求失败，%.1fs 后重试 (%d/%d): %s",
                    delay, attempt + 1, _MAX_RETRIES, _scrub_secrets(str(exc)),
                )
                time.sleep(delay)
        raise last_exc  # unreachable, but keeps type-checker happy

    @staticmethod
    def _extract_reply_text(response: object) -> str:
        try:
            choices = response.choices  # type: ignore[attr-defined]
            if not choices:
                raise ValueError("响应中不包含任何 choices")
            content = choices[0].message.content
            if not content:
                raise ValueError("响应中 assistant 消息内容为空")
        except (AttributeError, IndexError, ValueError) as exc:
            raise LLMResponseFormatError(
                f"LLM 响应格式异常，无法从中解析出 assistant 回复：{exc}"
            ) from exc
        return content
