"""Memory tools (hierarchical memory): ``remember`` and ``recall``.

``remember`` has safety_level ``write`` — it goes through approval.
``recall`` has safety_level ``read_only`` — no approval required.

Both tools receive a ``MemoryStore`` instance via closure so that the
caller controls which store to operate on.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from claw.memory.store import MEMORY_CATEGORIES, MemoryStore, MemoryStoreError
from claw.tools.base import Tool, ToolResult

# Maximum number of tags allowed per memory entry
_MAX_TAGS = 10
# Max tag length to prevent abuse
_MAX_TAG_LENGTH = 50


# =============================================================================
# Handlers
# =============================================================================


def _make_remember_handler(
    memory_store: MemoryStore,
    session_id_provider: Callable[[], str] | None = None,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        category: str = args["category"]
        content: str = args["content"]
        tags: list[str] = args.get("tags", [])
        importance: int = args.get("importance", 3)

        # Validate tags
        if not isinstance(tags, list):
            return ToolResult(
                ok=False,
                error="tags 必须是字符串数组",
            )
        if len(tags) > _MAX_TAGS:
            return ToolResult(
                ok=False,
                error=f"tags 最多 {_MAX_TAGS} 个，实际提供了 {len(tags)} 个",
            )
        for t in tags:
            if not isinstance(t, str) or not t.strip():
                return ToolResult(
                    ok=False,
                    error="tags 中的每个元素必须是非空字符串",
                )
            if len(t) > _MAX_TAG_LENGTH:
                return ToolResult(
                    ok=False,
                    error=f"标签 \"{t[:30]}...\" 超过最大长度 {_MAX_TAG_LENGTH}",
                )

        source_sid = ""
        if session_id_provider:
            try:
                source_sid = session_id_provider()
            except Exception:
                pass

        try:
            entry = memory_store.add(
                content=content,
                category=category,
                tags=tags,
                importance=importance,
                source_session_id=source_sid,
            )
        except MemoryStoreError as exc:
            return ToolResult(ok=False, error=str(exc))

        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "tool": "remember",
                    "memoryId": entry.memory_id,
                    "category": entry.category,
                    "content": entry.content,
                    "tags": entry.tags,
                    "importance": entry.importance,
                    "result": "记忆已保存",
                },
                ensure_ascii=False,
            ),
        )

    return handler


def _make_recall_handler(
    memory_store: MemoryStore,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        query: str = args["query"]
        category: str | None = args.get("category")
        limit: int = args.get("limit", 5)

        if category is not None and category not in MEMORY_CATEGORIES:
            return ToolResult(
                ok=False,
                error=(
                    f"无效的记忆类别: \"{category}\"，"
                    f"可选: {', '.join(sorted(MEMORY_CATEGORIES))}"
                ),
            )

        try:
            results = memory_store.recall(query=query, category=category, limit=limit)
        except MemoryStoreError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not results:
            return ToolResult(
                ok=True,
                content=json.dumps(
                    {
                        "tool": "recall",
                        "query": query,
                        "totalFound": 0,
                        "returned": 0,
                        "results": [],
                        "result": f"未找到与 \"{query}\" 相关的记忆。",
                    },
                    ensure_ascii=False,
                ),
            )

        result_items = [
            {
                "id": e.memory_id,
                "category": e.category,
                "content": e.content,
                "tags": e.tags,
                "importance": e.importance,
            }
            for e in results
        ]

        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "tool": "recall",
                    "query": query,
                    "totalFound": len(results),
                    "returned": len(results),
                    "results": result_items,
                },
                ensure_ascii=False,
            ),
        )

    return handler


# =============================================================================
# Tool definition factories
# =============================================================================


def create_remember_tool(
    memory_store: MemoryStore,
    session_id_provider: Callable[[], str] | None = None,
) -> Tool:
    return Tool(
        name="remember",
        description=(
            "保存一条结构化记忆到长期记忆中，跨 session 持久保留。"
            "当你发现用户透露了值得长期保留的重要信息（如项目名、技术栈、"
            "偏好、决策）时，调用此工具。"
            "需要提供 category（类别）和 content（记忆内容）。"
            "可选提供 tags（标签列表）和 importance（重要性 1-5，默认 3）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": sorted(MEMORY_CATEGORIES),
                    "description": (
                        "记忆类别：user_preference（用户偏好）、"
                        "project（项目信息）、decision（决策记录）、"
                        "fact（一般事实）、general（其他）"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "要记住的事实，简洁明确的一句话",
                    "minLength": 1,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表，用于后续检索（可选，最多10个）",
                },
                "importance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "重要性 1-5，默认 3",
                },
            },
            "required": ["category", "content"],
        },
        handler=_make_remember_handler(memory_store, session_id_provider),
        safety_level="read_only",
    )


def create_recall_tool(
    memory_store: MemoryStore,
) -> Tool:
    return Tool(
        name="recall",
        description=(
            "检索长期记忆，返回与查询最相关的记忆条目。"
            "当你需要回忆用户之前提到的信息、偏好、项目或决策时，"
            "调用此工具。返回结果按相关性排序。"
            "不要猜测或编造记忆中的内容——始终通过此工具确认。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                    "minLength": 1,
                },
                "category": {
                    "type": "string",
                    "enum": sorted(MEMORY_CATEGORIES),
                    "description": "限定检索类别（可选）",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "返回条数上限，默认 5",
                },
            },
            "required": ["query"],
        },
        handler=_make_recall_handler(memory_store),
        safety_level="read_only",
    )
