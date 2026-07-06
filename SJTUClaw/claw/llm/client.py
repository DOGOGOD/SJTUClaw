"""Minimal LLM API client.

Wraps an OpenAI-compatible chat completions endpoint with a single
`chat(messages) -> str` entry point, translating low-level failures
into clear, user-facing errors.
"""

from __future__ import annotations

from typing import Iterable, Mapping

import openai
from openai import OpenAI

from claw.config import LLMConfig

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
        messages_list = list(messages)
        try:
            response = self._client.chat.completions.create(
                model=self._config.model,
                messages=messages_list,  # type: ignore[arg-type]
            )
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

        return self._extract_reply_text(response)

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
