"""Context budget tracker: knows how many tokens the assembled context
consumes and how many remain before hitting the model's window limit.

Usage::

    budget = ContextBudget.measure(
        system_prompt=..., soul=..., memory_block=...,
        tool_defs_text=..., skill_block=..., summary_block=...,
        messages=...,
        max_tokens=25600,
    )
    if budget.usage_ratio > 0.80:
        # trigger compaction
"""

from __future__ import annotations

from dataclasses import dataclass

from claw.context.token_counter import count_tokens, count_tokens_for_messages
from claw.session.models import Message


class ContextOverflowError(RuntimeError):
    """Raised when context exceeds the model window by more than 5 %.

    Callers should either compact the session or refuse the request
    rather than sending it and letting the model silently truncate.
    """


@dataclass(frozen=True)
class ContextBudget:
    """Immutable snapshot of context token consumption.

    All token counts are computed once at construction time via
    ``ContextBudget.measure()``.
    """

    max_tokens: int
    system_prompt_tokens: int
    soul_tokens: int
    memory_block_tokens: int
    tool_defs_tokens: int
    skill_index_tokens: int
    summary_tokens: int
    messages_tokens: int

    # -- derived ----------------------------------------------------------

    @property
    def fixed_overhead_tokens(self) -> int:
        """Tokens consumed by non-message context (system prompt, soul,
        memory, tools, skills, summary)."""
        return (
            self.system_prompt_tokens
            + self.soul_tokens
            + self.memory_block_tokens
            + self.tool_defs_tokens
            + self.skill_index_tokens
            + self.summary_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.fixed_overhead_tokens + self.messages_tokens

    @property
    def available_tokens(self) -> int:
        """Remaining token budget.  Negative means over the limit."""
        return self.max_tokens - self.total_tokens

    @property
    def usage_ratio(self) -> float:
        """0.0–1.0+; >1.0 means the context already exceeds the budget."""
        if self.max_tokens <= 0:
            return 0.0
        return self.total_tokens / self.max_tokens

    # -- factory ----------------------------------------------------------

    @classmethod
    def measure(
        cls,
        *,
        max_tokens: int,
        system_prompt: str = "",
        soul: str = "",
        memory_block: str | None = None,
        tool_defs_text: str = "",
        skill_block: str | None = None,
        summary_block: str | None = None,
        messages: list[Message] | None = None,
    ) -> "ContextBudget":
        """Build a budget snapshot from the raw context components."""
        return cls(
            max_tokens=max_tokens,
            system_prompt_tokens=count_tokens(system_prompt),
            soul_tokens=count_tokens(soul),
            memory_block_tokens=count_tokens(memory_block or ""),
            tool_defs_tokens=count_tokens(tool_defs_text),
            skill_index_tokens=count_tokens(skill_block or ""),
            summary_tokens=count_tokens(summary_block or ""),
            messages_tokens=(
                count_tokens_for_messages(messages) if messages else 0
            ),
        )

    # -- safety check -----------------------------------------------------

    def check_overflow(self) -> None:
        """Raise ``ContextOverflowError`` if context exceeds 105 % of budget.

        Call this just before sending the request to the LLM as a
        last-mile safety net.  Values between 100 % and 105 % only log
        a warning — the model will likely still handle them.
        """
        if self.usage_ratio >= 1.05:
            raise ContextOverflowError(
                f"上下文 token 数 ({self.total_tokens}) 已超过模型窗口预算"
                f" ({self.max_tokens}) 的 105%，拒绝发送请求以避免静默截断。"
                f"请先执行 /compact 压缩会话历史，或增大 LLM_CONTEXT_WINDOW 配置。"
            )
