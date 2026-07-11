"""Agent loop — the single, unified entry point for all claw interactions.

``run_agent_turn(session_id, user_message, ...)`` is the **only** place
where the LLM is called as part of a user-facing conversation. Every
entry point (CLI, Gateway, Scheduler, …) must route through this
function and must never call ``LLMClient`` directly.

Loop flow::

    user message
      -> append to session
      -> loop:
           buildContext (messages + tool definitions + skill index)
           -> callLLM
           -> if final:
                save assistant message -> return
           -> if use_skill (skill_select):
                create skill approval -> user confirms/denies
                if approved: inject skill content -> continue
                if rejected: record rejection -> continue
           -> if write / shell tool:
                create approval -> wait for user decision
                if rejected: record rejection -> continue
                execute tool, save result
           -> if download / read-only:
                execute tool, save result
           -> continue loop

``use_skill`` is a special tool with safety_level ``skill_select``.
When the model invokes it, the agent loop pauses for user confirmation
BEFORE the skill content enters the LLM context — the model only sees
the full instructions after the user approves.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient
from claw.session.store import SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry


_APPROVAL_REQUIRED_LEVELS = {"write", "shell"}
_SKILL_SELECT_LEVEL = "skill_select"
_MAX_AGENT_ITERATIONS = 15  # safety cap: prevent infinite tool-calling loops
_MAX_REJECTIONS_PER_OPERATION = 3  # stop LLM from retrying the same rejected operation


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent_turn(
    session_id: str,
    user_message: str,
    *,
    session_store: SessionStore,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
    llm_client: LLMClient,
    approval_handler: Callable | None = None,
    skill_registry=None,
    skill_source: str = "",
    skill_name: str = "",
    auto_reason: str = "",
    compaction_worker=None,
    auto_mode: bool = False,
) -> str:
    """Run a single agent turn: user message in, assistant reply out.

    Args:
        session_id: target session.
        user_message: the user's input text.
        session_store: session persistence.
        context_builder: assembles the LLM ``messages`` array.
        tool_registry: registered tools (must include ``use_skill`` if
            ``skill_registry`` is provided).
        llm_client: the LLM API wrapper.
        approval_handler: optional callable
            ``(approval_request) -> ApprovalRequest``.
        skill_registry: optional ``SkillRegistry`` for skill injection.
        skill_source: ``"explicit"`` if the user typed ``/skill name task``,
            ``"auto"`` if the model selected the skill, ``""`` otherwise.
        skill_name: if *skill_source* is set, the pre-selected skill name.
        auto_reason: if *skill_source* == ``"auto"``, the model's reason.
        compaction_worker: optional ``CompactionWorker`` for async
            background compaction after the turn completes.

    Returns:
        The assistant's final reply text.
    """

    def _maybe_async_compact(_sess) -> None:
        """Background compaction is handled by the idle compaction worker.
        (nanobot-style: only compact idle sessions, not every turn.)"""
        pass

    def _needs_approval(tool_name: str) -> bool:
        t = tool_registry.get_tool(tool_name)
        return t is not None and t.safety_level in _APPROVAL_REQUIRED_LEVELS

    def _is_skill_select(tool_name: str) -> bool:
        t = tool_registry.get_tool(tool_name)
        return t is not None and t.safety_level == _SKILL_SELECT_LEVEL

    session = session_store.get(session_id)

    # Per-turn rejection tracker — prevents LLM from retrying the same
    # rejected operation endlessly.  Key = "tool_name:args_json".
    _rejection_tracker: dict[str, int] = {}

    # -- 1. If this turn is an explicit /skill invocation, inject skill
    #      content right away (user already chose the skill).
    if skill_source == "explicit" and skill_name:
        injected = context_builder.build_skill_injection_message(
            skill_name, user_message
        )
        session.append_message("user", injected)
        _record_skill_usage(
            session, skill_name, user_message, "explicit", ""
        )
    else:
        session.append_message("user", user_message)

    _save_safe(session_store, session)

    # Track whether we've already handled skill selection in this turn
    skill_already_injected_for_turn = skill_source == "explicit"

    # -- 2. Think → Act → Observe loop ---------------------------------------
    turn_count = 0
    while True:
        turn_count += 1
        if turn_count > _MAX_AGENT_ITERATIONS:
            print(f"[agent] 达到最大迭代次数 {_MAX_AGENT_ITERATIONS}，强制结束本回合")
            final_text = (
                "本轮任务涉及的操作过多，已达到最大工具调用次数限制。"
                "请尝试将任务拆分为更小的步骤，或检查是否需要调整方案。"
            )
            session.append_message("assistant", final_text)
            _save_safe(session_store, session)
            _maybe_async_compact(session)
            return final_text

        messages = context_builder.build_messages(
            session,
            tool_registry=tool_registry,
            include_tool_instructions=True,
        )
        tool_defs = context_builder.get_tool_definitions(tool_registry)

        response = llm_client.chat_with_tools(messages, tool_defs)

        # -- Final answer ----------------------------------------------------
        if response.is_final and response.final is not None:
            final_text = response.final
            session.append_message("assistant", final_text)
            _save_safe(session_store, session)
            _maybe_async_compact(session)
            return final_text

        # -- Tool call(s) ----------------------------------------------------
        if response.is_tool_call:
            for tc in response.tool_calls:
                args_json = json.dumps(tc.args, ensure_ascii=False)
                print(f"[tool_call #{turn_count}] {tc.name} {args_json}")

                # ---- use_skill (second call already handled) -------------------
                if _is_skill_select(tc.name) and skill_already_injected_for_turn:
                    msg = (
                        f"skill \"{skill_name or '?'}\" 已在本轮加载，"
                        f"无需重复调用 use_skill。请直接继续执行任务。"
                    )
                    session.append_message(
                        "tool",
                        json.dumps(
                            {"tool": "use_skill", "ok": True, "result": msg},
                            ensure_ascii=False,
                        ),
                    )
                    print(f"[tool_result] use_skill: {msg}")
                    continue

                # ---- use_skill (skill_select) -------------------------------
                if _is_skill_select(tc.name) and not skill_already_injected_for_turn:
                    result = _handle_skill_select(
                        tc.args,
                        session,
                        session_store,
                        context_builder,
                        approval_handler,
                        session_id,
                        skill_registry,
                    )
                    if result is not None:
                        # Rejected or failed — record but keep the door open
                        session.append_message("tool", result)
                        _save_safe(session_store, session)
                    else:
                        # Successfully injected
                        skill_already_injected_for_turn = True
                    continue

                # ---- Approval gate for write / shell tools ------------------
                if _needs_approval(tc.name) and approval_handler is not None:
                    from claw.approval.manager import ApprovalStatus

                    if auto_mode:
                        # AUTO mode: skip approval, tool operates freely within workspace.
                        # Workspace boundary is still enforced by the tool handlers.
                        print(f"[auto] 自动批准 {tc.name}（AUTO 模式）")
                    else:
                        req = _make_approval_request(session_id, tc.name, tc.args)
                        decided = approval_handler(req)

                        if decided is None or decided.status == ApprovalStatus.REJECTED.value:
                            reason = (
                                decided.reject_reason
                                if decided and decided.reject_reason
                                else "用户拒绝了该操作"
                            )
                            # Track consecutive rejections to break retry loops
                            rejection_key = f"{tc.name}:{json.dumps(tc.args, sort_keys=True, ensure_ascii=False)}"
                            rejection_count = _rejection_tracker.get(rejection_key, 0) + 1
                            _rejection_tracker[rejection_key] = rejection_count

                            if rejection_count >= 3:
                                result_content = json.dumps({
                                    "tool": tc.name,
                                    "ok": False,
                                    "result": (
                                        f"操作已被用户连续拒绝 {rejection_count} 次。"
                                        f"请停止尝试此操作，改用其他方式完成任务，或向用户说明情况。"
                                    ),
                                }, ensure_ascii=False)
                            else:
                                result_content = json.dumps({
                                    "tool": tc.name,
                                    "ok": False,
                                    "result": (
                                        f"操作被用户拒绝（第 {rejection_count} 次）：{reason}。"
                                        f"这是用户主动拒绝，并非工具执行失败。请不要用相同方式重试。"
                                        f"如需继续，请调整方案或向用户说明原因。"
                                    ),
                                }, ensure_ascii=False)
                            session.append_message("tool", result_content)
                            print(f"[tool_result] {tc.name}: 被拒绝 ({rejection_count}/3) — {reason}")
                            continue

                # ---- Execute the tool ---------------------------------------
                result = tool_registry.execute_by_name(tc.name, tc.args)

                if result.ok:
                    result_content = result.content or "(空结果)"
                    print(f"[tool_result] {tc.name}: 成功")
                else:
                    result_content = json.dumps(
                        {"tool": tc.name, "ok": False, "result": f"错误: {result.error}"},
                        ensure_ascii=False,
                    )
                    print(f"[tool_result] {tc.name}: 失败 — {result.error}")

                tool_msg_content = result.content if (result.ok and result.content) else result_content
                session.append_message("tool", tool_msg_content)

                # If this was an overwrite_file and skill was active,
                # record the output path
                if tc.name == "overwrite_file" and skill_name:
                    _update_skill_output(session, tc.args.get("path", ""))

            _save_safe(session_store, session)
            continue

        # -- Neither final nor tool_call ------------------------------------
        empty_reply = ""
        session.append_message("assistant", empty_reply)
        _save_safe(session_store, session)
        _maybe_async_compact(session)
        return empty_reply


# ---------------------------------------------------------------------------
# Skill selection handler
# ---------------------------------------------------------------------------


def _handle_skill_select(
    args: dict[str, Any],
    session,
    session_store: SessionStore,
    context_builder: ContextBuilder,
    approval_handler: Callable | None,
    session_id: str,
    skill_registry,
) -> str | None:
    """Handle a ``use_skill`` tool call.

    1. Resolve the skill name from args.
    2. If approval_handler is provided, create a skill approval and wait.
    3. If approved (or no handler), inject the skill content.
    4. Record skill usage.

    Returns:
        A tool-observation JSON string to append to the session, or None
        if the skill was approved and injected (in which case the caller
        continues the loop).
    """
    from claw.approval.manager import ApprovalRequest, ApprovalStatus

    skill_name = args.get("skill_name", "").strip()
    auto_reason = args.get("reason", "").strip()

    if not skill_name:
        return json.dumps(
            {"tool": "use_skill", "ok": False, "result": "错误: 未指定 skill 名称"},
            ensure_ascii=False,
        )

    # Verify skill exists
    if skill_registry is None or skill_registry.get_skill(skill_name) is None:
        return json.dumps(
            {"tool": "use_skill", "ok": False, "result": f"错误: 未找到 skill \"{skill_name}\""},
            ensure_ascii=False,
        )

    # -- Skill approval ------------------------------------------------------
    if approval_handler is not None:
        req = ApprovalRequest(
            session_id=session_id,
            tool_name="use_skill",
            tool_args={
                "skill_name": skill_name,
                "reason": auto_reason or "模型自主判断该任务适合此 skill",
            },
        )
        decided = approval_handler(req)

        if decided is None or decided.status == ApprovalStatus.REJECTED.value:
            reason = (
                decided.reject_reason
                if decided and decided.reject_reason
                else "用户拒绝了该 skill 的使用"
            )
            print(f"[skill] {skill_name}: 用户拒绝 — {reason}")
            return json.dumps(
                {"tool": "use_skill", "ok": False, "result": f"skill 使用被拒绝: {reason}"},
                ensure_ascii=False,
            )

    # -- Inject skill content ------------------------------------------------
    print(f"[skill] {skill_name}: 已批准，注入 skill 内容")

    # Get the original user task (the first user message of this turn)
    user_task = ""
    for m in reversed(session.messages):
        if m.role == "user":
            user_task = m.content
            break

    injected = context_builder.build_skill_injection_message(skill_name, user_task)
    session.append_message("user", injected)

    _record_skill_usage(session, skill_name, user_task, "auto", auto_reason)
    _save_safe(session_store, session)

    return None  # Signal success — the loop continues with skill content injected


# ---------------------------------------------------------------------------
# Skill usage tracking
# ---------------------------------------------------------------------------


def _record_skill_usage(
    session,
    skill_name: str,
    user_task: str,
    source: str,
    auto_reason: str,
) -> None:
    """Append a skill usage record to *session.skill_usage*."""
    from claw.skills.registry import SkillUsageRecord

    record = SkillUsageRecord(
        skill_name=skill_name,
        session_id=session.session_id,
        user_task=user_task[:500],  # truncate long tasks
        source=source,
        auto_reason=auto_reason,
    )
    session.skill_usage.append(record.to_dict())


def _update_skill_output(session, path: str) -> None:
    """Update the latest skill usage record with the output path."""
    if not session.skill_usage or not path:
        return
    session.skill_usage[-1]["outputPath"] = path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_approval_request(session_id: str, tool_name: str, tool_args: dict):
    from claw.approval.manager import ApprovalRequest
    return ApprovalRequest(
        session_id=session_id,
        tool_name=tool_name,
        tool_args=tool_args,
    )


def _save_safe(session_store: SessionStore, session) -> None:
    try:
        session_store.save(session)
    except SessionStoreError as exc:
        print(f"[警告] session 保存失败: {exc}")
