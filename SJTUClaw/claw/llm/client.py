"""Minimal LLM API client.

Wraps an OpenAI-compatible chat completions endpoint with two entry
points:

    chat(messages) -> str                 # simple text-only call
    chat_with_tools(messages, tools) -> AgentResponse   # tool-calling call

Both translate low-level failures into clear, user-facing errors.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import openai
from openai import OpenAI

from claw.config import LLMConfig
from claw.llm.protocol import AgentResponse, ProtocolParseError, parse_agent_response

Message = Mapping[str, str]


class LLMError(RuntimeError):
    """Base class for all LLM-related errors shown to the user."""


class LLMConnectionError(LLMError):
    """Raised when the LLM API could not be reached (network failure)."""


class LLMResponseStatusError(LLMError):
    """Raised when the LLM API responds with an HTTP error status."""


class LLMResponseFormatError(LLMError):
    """Raised when the LLM API response body cannot be parsed as expected."""


class LLMClient:
    """Thin wrapper around an OpenAI-compatible chat completion API."""

    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    # -- simple text call (backward-compatible) -------------------------------

    def chat(self, messages: Iterable[Message]) -> str:
        """Send `messages` to the LLM and return the assistant's reply text.

        Args:
            messages: an ordered list of `{"role": ..., "content": ...}`
                dicts, following the usual chat completion convention.

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
        response = self._call_api(list(messages))
        return self._extract_reply_text(response)

    # -- tool-calling call ---------------------------------------------------

    def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        tool_definitions: list[dict[str, Any]] | None = None,
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

        Returns:
            ``AgentResponse`` with either ``final`` text or ``tool_calls``.

        Raises:
            LLMConnectionError / LLMResponseStatusError /
                LLMResponseFormatError / LLMError: on API-level failures.
        """
        response = self._call_api(messages, tool_definitions=tool_definitions)
        choice = response.choices[0]

        # Extract native tool calls if present
        native_tool_calls: list[dict[str, Any]] | None = None
        msg = choice.message
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            native_tool_calls = [
                {
                    "id": tc.id if hasattr(tc, "id") else "",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        content_text: str | None = getattr(msg, "content", None) or None

        try:
            return parse_agent_response(content_text, native_tool_calls)
        except ProtocolParseError as exc:
            raise LLMResponseFormatError(
                f"LLM 响应协议解析失败：{exc}\n"
                f"原始内容（前200字符）：{str(content_text or '')[:200]}"
            ) from exc

    # -- internal helpers ----------------------------------------------------

    def _call_api(
        self,
        messages: list[dict[str, str]],
        tool_definitions: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Call the chat completions API and return the raw response."""
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,  # type: ignore[arg-type]
        }
        if tool_definitions:
            kwargs["tools"] = tool_definitions  # type: ignore[arg-type]
            # Let the model decide when to call tools; don't force it
            # kwargs["tool_choice"] = "auto"

        try:
            return self._client.chat.completions.create(**kwargs)
        except openai.APIConnectionError as exc:
            raise LLMConnectionError(
                "无法连接到 LLM 服务，请检查网络连接以及 LLM_BASE_URL 配置是否正确。\n"
                f"原始错误：{exc}"
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMResponseStatusError(
                f"LLM 服务返回了错误状态码 {exc.status_code}。\n"
                "请检查 LLM_API_KEY、LLM_MODEL、LLM_BASE_URL 是否正确、是否有权限访问该模型。\n"
                f"原始错误：{exc.message}"
            ) from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"调用 LLM 服务时发生未知错误：{exc}") from exc

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
