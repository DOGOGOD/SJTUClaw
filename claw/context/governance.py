"""Context governance: prepare messages for the model by repairing, normalizing,

and compacting them in-flight — inspired by ContextGovernor pattern.



This module is the **single gateway** through which all model-facing

messages pass before being sent to the LLM.  The persisted session

history is never mutated; only a copy is prepared.



Pipeline (applied in order):



1.  Strip placeholder assistant messages that carry no useful context.

2.  Strip malformed tool calls whose name is missing/non-string.

3.  Drop orphan tool results (no matching assistant tool_call).

4.  Backfill missing tool results (assistant tool_call with no result).

5.  Apply tool-result budget (truncate oversized results).

6.  Compact in-flight overflow (summarize large tool results to stay

    within context window).

7.  Snip history from the front when it still overflows after all

    other measures.

8.  Drop orphans / backfill again (snip may have broken pairs).

"""



from __future__ import annotations



from dataclasses import dataclass

from typing import Any



from claw.context.token_counter import count_tokens, count_tokens_for_messages

from claw.session.models import Message



# ---------------------------------------------------------------------------

# Constants

# ---------------------------------------------------------------------------



SNIP_SAFETY_BUFFER = 1024

INFLIGHT_COMPACT_TARGET_RATIO = 0.85

MICROCOMPACT_KEEP_RECENT = 10

MICROCOMPACT_MIN_CHARS = 500



# Tools whose results are safe to replace with a short summary when the

# context is overflowing.  These are typically read-only tools that

# produce large outputs.

COMPACTABLE_TOOLS = frozenset({

    "read", "exec", "grep", "find_files",

    "web_search", "web_fetch", "list_dir",

})



# Tools exempt from tool-result offloading (read_file is the recovery

# path for persisted results — offloading it risks loops).

TOOL_RESULT_EXEMPT_TOOLS = frozenset({"read_file", "read"})



PLACEHOLDER_TEXTS = frozenset({

    "[Previous assistant message omitted.]",

})



BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"





# ---------------------------------------------------------------------------

# Helpers

# ---------------------------------------------------------------------------





def _tool_call_name_is_valid(tool_call: Any) -> bool:

    """Return True if *tool_call* carries a usable tool name."""

    if not isinstance(tool_call, dict):

        return False

    fn = tool_call.get("function")

    name = fn.get("name") if isinstance(fn, dict) else tool_call.get("name")

    return isinstance(name, str) and bool(name)





# ---------------------------------------------------------------------------

# Config

# ---------------------------------------------------------------------------





@dataclass(slots=True)

class GovernanceConfig:

    """Immutable configuration snapshot for one governance pass."""



    max_tool_result_chars: int

    context_window_tokens: int | None = None

    max_output_tokens: int = 4096

    inflight_start_index: int = 0  # boundary in messages where this turn started

    session_key: str | None = None





# ---------------------------------------------------------------------------

# Governor

# ---------------------------------------------------------------------------





class ContextGovernor:

    """Prepare model-copy messages while preserving persisted history.



    Every method that transforms messages returns a **new list** when

    changes were made, or the original list when nothing changed.

    """



    def prepare_for_model(

        self,

        config: GovernanceConfig,

        messages: list[dict[str, Any]],

        compacted_tool_call_ids: set[str] | None = None,

    ) -> list[dict[str, Any]]:

        """Apply the full governance pipeline and return model-ready messages."""

        cids: set[str] = set(compacted_tool_call_ids) if compacted_tool_call_ids else set()



        updated = self._strip_placeholder_assistant_messages(messages)

        updated = self._strip_malformed_tool_calls(updated)

        updated = self._drop_orphan_tool_results(updated)

        updated = self._backfill_missing_tool_results(updated)

        updated = self._apply_tool_result_budget(config, updated)

        updated = self._compact_inflight_overflow(config, updated, cids)

        updated = self._snip_history(config, updated)

        updated = self._drop_orphan_tool_results(updated)

        return self._backfill_missing_tool_results(updated)



    @staticmethod

    def input_budget(config: GovernanceConfig) -> int:

        """Compute the safe input token budget."""

        if not config.context_window_tokens:

            return 0

        budget = config.context_window_tokens - config.max_output_tokens - SNIP_SAFETY_BUFFER

        return budget if budget > 0 else 0



    # ------------------------------------------------------------------

    # Step 1: Strip placeholder assistant messages

    # ------------------------------------------------------------------



    @staticmethod

    def _strip_placeholder_assistant_messages(

        messages: list[dict[str, Any]],

    ) -> list[dict[str, Any]]:

        updated: list[dict[str, Any]] | None = None

        for idx, msg in enumerate(messages):

            if msg.get("role") != "assistant":

                if updated is not None:

                    updated.append(msg)

                continue

            content = msg.get("content", "")

            text = content if isinstance(content, str) else ""

            is_placeholder = text.strip() in PLACEHOLDER_TEXTS

            has_tool_calls = bool(msg.get("tool_calls"))

            if is_placeholder and not has_tool_calls:

                if updated is None:

                    updated = list(messages[:idx])

                continue

            if updated is not None:

                updated.append(msg)

        return messages if updated is None else updated



    # ------------------------------------------------------------------

    # Step 2: Strip malformed tool calls

    # ------------------------------------------------------------------



    @staticmethod

    def _strip_malformed_tool_calls(

        messages: list[dict[str, Any]],

    ) -> list[dict[str, Any]]:

        updated: list[dict[str, Any]] | None = None

        for idx, msg in enumerate(messages):

            if msg.get("role") != "assistant":

                if updated is not None:

                    updated.append(msg)

                continue

            calls = msg.get("tool_calls")

            if not calls:

                if updated is not None:

                    updated.append(msg)

                continue

            kept = [tc for tc in calls if _tool_call_name_is_valid(tc)]

            if len(kept) == len(calls):

                if updated is not None:

                    updated.append(msg)

                continue

            if updated is None:

                updated = [dict(m) for m in messages[:idx]]

            repaired = dict(msg)

            if kept:

                repaired["tool_calls"] = kept

            else:

                repaired.pop("tool_calls", None)

            has_content = bool(repaired.get("content"))

            if not kept and not has_content:

                continue

            updated.append(repaired)

        return messages if updated is None else updated



    # ------------------------------------------------------------------

    # Step 3: Drop orphan tool results

    # ------------------------------------------------------------------



    @staticmethod

    def _drop_orphan_tool_results(

        messages: list[dict[str, Any]],

    ) -> list[dict[str, Any]]:

        declared: set[str] = set()

        updated: list[dict[str, Any]] | None = None

        for idx, msg in enumerate(messages):

            role = msg.get("role")

            if role == "assistant":

                for tc in msg.get("tool_calls") or []:

                    if isinstance(tc, dict) and tc.get("id"):

                        declared.add(str(tc["id"]))

            if role == "tool":

                tid = msg.get("tool_call_id")

                if tid and str(tid) not in declared:

                    if updated is None:

                        updated = [dict(m) for m in messages[:idx]]

                    continue

            if updated is not None:

                updated.append(dict(msg))

        return messages if updated is None else updated



    # ------------------------------------------------------------------

    # Step 4: Backfill missing tool results

    # ------------------------------------------------------------------



    @staticmethod

    def _backfill_missing_tool_results(

        messages: list[dict[str, Any]],

    ) -> list[dict[str, Any]]:

        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)

        fulfilled: set[str] = set()

        for idx, msg in enumerate(messages):

            role = msg.get("role")

            if role == "assistant":

                for tc in msg.get("tool_calls") or []:

                    if isinstance(tc, dict) and tc.get("id"):

                        name = ""

                        func = tc.get("function")

                        if isinstance(func, dict):

                            name = func.get("name", "")

                        declared.append((idx, str(tc["id"]), name))

            elif role == "tool":

                tid = msg.get("tool_call_id")

                if tid:

                    fulfilled.add(str(tid))



        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]

        if not missing:

            return messages



        updated = list(messages)

        offset = 0

        for assistant_idx, call_id, name in missing:

            insert_at = assistant_idx + 1 + offset

            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":

                insert_at += 1

            updated.insert(insert_at, {

                "role": "tool",

                "tool_call_id": call_id,

                "name": name,

                "content": BACKFILL_CONTENT,

            })

            offset += 1

        return updated



    # ------------------------------------------------------------------

    # Step 5: Apply tool-result budget

    # ------------------------------------------------------------------



    @staticmethod

    def _apply_tool_result_budget(

        config: GovernanceConfig,

        messages: list[dict[str, Any]],

    ) -> list[dict[str, Any]]:

        updated = messages

        for idx, msg in enumerate(messages):

            if msg.get("role") != "tool":

                continue

            content = msg.get("content")

            if not isinstance(content, str):

                continue

            if len(content) > config.max_tool_result_chars:

                if updated is messages:

                    updated = [dict(m) for m in messages]

                updated[idx]["content"] = (

                    content[:config.max_tool_result_chars]

                    + "\n...[truncated]"

                )

        return updated



    # ------------------------------------------------------------------

    # Step 6: In-flight compaction of tool results

    # ------------------------------------------------------------------



    def _compact_inflight_overflow(

        self,

        config: GovernanceConfig,

        messages: list[dict[str, Any]],

        compacted_tool_call_ids: set[str],

    ) -> list[dict[str, Any]]:

        budget = self.input_budget(config)

        if budget <= 0:

            return messages



        updated = self._apply_recorded_compactions(messages, compacted_tool_call_ids)

        estimate = _estimate_message_list_tokens(updated)

        if estimate <= budget:

            return updated



        target = int(budget * INFLIGHT_COMPACT_TARGET_RATIO)

        candidates = self._inflight_compaction_candidates(config, updated, compacted_tool_call_ids)

        if not candidates:

            return updated



        for candidate_idx, (idx, tool_call_id) in enumerate(candidates):

            is_newest = candidate_idx == len(candidates) - 1

            if is_newest and estimate <= budget:

                break

            if tool_call_id in compacted_tool_call_ids:

                continue

            if updated is messages:

                updated = [dict(m) for m in messages]

            compacted_tool_call_ids.add(tool_call_id)

            updated[idx]["content"] = self._summary_for(updated[idx])

            estimate = _estimate_message_list_tokens(updated)

            if estimate <= target:

                break



        return updated



    @staticmethod

    def _summary_for(message: dict[str, Any]) -> str:

        name = message.get("name", "tool")

        return f"[Prior {name} result compacted to fit context; the tool call already completed.]"



    @staticmethod

    def _apply_recorded_compactions(

        messages: list[dict[str, Any]],

        compacted_tool_call_ids: set[str],

    ) -> list[dict[str, Any]]:

        if not compacted_tool_call_ids:

            return messages

        updated = messages

        for idx, msg in enumerate(messages):

            if msg.get("role") != "tool":

                continue

            tid = msg.get("tool_call_id")

            if not tid or str(tid) not in compacted_tool_call_ids:

                continue

            summary = ContextGovernor._summary_for(msg)

            if msg.get("content") == summary:

                continue

            if updated is messages:

                updated = [dict(m) for m in messages]

            updated[idx]["content"] = summary

        return updated



    def _inflight_compaction_candidates(

        self,

        config: GovernanceConfig,

        messages: list[dict[str, Any]],

        compacted_tool_call_ids: set[str],

    ) -> list[tuple[int, str]]:

        compactable: list[tuple[int, str]] = []

        for idx, msg in enumerate(messages):

            if idx < config.inflight_start_index:

                continue

            if msg.get("role") != "tool":

                continue

            if msg.get("name") not in COMPACTABLE_TOOLS:

                continue

            tid = msg.get("tool_call_id")

            if not tid or str(tid) in compacted_tool_call_ids:

                continue

            content = msg.get("content")

            if not isinstance(content, str) or len(content) < MICROCOMPACT_MIN_CHARS:

                continue

            compactable.append((idx, str(tid)))



        if not compactable:

            return []

        primary_count = max(0, len(compactable) - MICROCOMPACT_KEEP_RECENT)

        # Keep newest uncompacted; compact oldest first.

        return compactable[:primary_count] + compactable[primary_count:]



    # ------------------------------------------------------------------

    # Step 7: Snip history from the front

    # ------------------------------------------------------------------



    @staticmethod

    def _snip_history(

        config: GovernanceConfig,

        messages: list[dict[str, Any]],

    ) -> list[dict[str, Any]]:

        if not messages or not config.context_window_tokens:

            return messages



        budget = ContextGovernor.input_budget(config)

        if budget <= 0:

            return messages



        estimate = _estimate_message_list_tokens(messages)

        if estimate <= budget:

            return messages



        system_msgs = [dict(m) for m in messages if m.get("role") == "system"]

        non_system = [dict(m) for m in messages if m.get("role") != "system"]

        if not non_system:

            return messages



        system_tokens = sum(_estimate_msg_tokens(m) for m in system_msgs)

        remaining_budget = max(0, budget - system_tokens)



        kept: list[dict[str, Any]] = []

        kept_tokens = 0

        for msg in reversed(non_system):

            mt = _estimate_msg_tokens(msg)

            if kept and kept_tokens + mt > remaining_budget:

                break

            kept.append(msg)

            kept_tokens += mt

        kept.reverse()



        # Always anchor to at least one user message

        kept = ContextGovernor._user_tail(kept) or ContextGovernor._user_tail(non_system, last=True) or kept

        # Safety net: if after snipping nothing survived (e.g. a single

        # very large message exceeded the budget), keep at least the most

        # recent non-system message so the LLM receives some context.

        if not kept and non_system:

            kept = [non_system[-1]]

        return system_msgs + kept



    @staticmethod

    def _user_tail(messages: list[dict[str, Any]], *, last: bool = False) -> list[dict[str, Any]]:

        indexes = range(len(messages) - 1, -1, -1) if last else range(len(messages))

        for idx in indexes:

            if messages[idx].get("role") == "user":

                return messages[idx:]

        return []





# ---------------------------------------------------------------------------

# Token estimation helpers

# ---------------------------------------------------------------------------





def _estimate_msg_tokens(msg: dict[str, Any]) -> int:

    """Estimate tokens for a single message dict."""

    content = msg.get("content", "")

    if isinstance(content, str):

        return max(1, count_tokens(content))

    if isinstance(content, list):

        total = 0

        for block in content:

            if isinstance(block, dict) and block.get("type") == "text":

                total += count_tokens(block.get("text", ""))

        return max(1, total)

    return 1





def _estimate_message_list_tokens(messages: list[dict[str, Any]]) -> int:

    """Estimate total tokens for a list of message dicts."""

    return sum(_estimate_msg_tokens(m) for m in messages)

