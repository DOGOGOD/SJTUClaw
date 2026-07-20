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


import logging


import os


import threading


import time

import uuid


from datetime import datetime, timezone


from pathlib import Path


from typing import Any, Callable





from claw.context.builder import ContextBuilder


from claw.agent.events import (


    ErrorEvent,


    FinalEvent,


    ThinkingEvent,


    ToolCallEndEvent,


    ToolCallStartEvent,


)


from claw.llm.client import LLMClient


from claw.session.store import SessionStore


from claw.tools.base import ToolRegistry, ToolResult





logger = logging.getLogger(__name__)





_APPROVAL_REQUIRED_LEVELS = {"write", "shell"}


_SKILL_SELECT_LEVEL = "skill_select"


def _positive_env_int(name: str, default: int) -> int:
    """Read a positive integer without making module import fragile."""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 不是整数，使用默认值 %d", name, raw, default)
        return default
    if value < 1:
        logger.warning("环境变量 %s=%r 必须大于 0，使用默认值 %d", name, raw, default)
        return default
    return value


_MAX_AGENT_ITERATIONS = _positive_env_int("CLAW_MAX_AGENT_ITERATIONS", 15)
_MAX_TOOL_CALLS_PER_TURN = _positive_env_int("CLAW_MAX_TOOL_CALLS_PER_TURN", 20)
_MAX_IDENTICAL_TOOL_CALLS = _positive_env_int("CLAW_MAX_IDENTICAL_TOOL_CALLS", 3)


_MAX_REJECTIONS_PER_OPERATION = 3  # stop LLM from retrying the same rejected operation








def _now_iso() -> str:


    return datetime.now(timezone.utc).isoformat(timespec="seconds")








# Per-session metrics aggregators (roll up turn stats for diagnostics)


_session_aggregators: dict[str, Any] = {}
_session_health_monitors: dict[str, Any] = {}
_metrics_lock = threading.Lock()
_MAX_METRIC_SESSIONS = 500








def get_session_metrics_summary(session_id: str) -> dict[str, Any] | None:


    """Return the aggregated metrics summary for *session_id*, or None."""


    with _metrics_lock:
        agg = _session_aggregators.get(session_id)
        monitor = _session_health_monitors.get(session_id)
    if agg is None:
        return None
    summary = agg.summary()
    if monitor is not None:
        summary["health"] = monitor.summary()
    return summary








# ---------------------------------------------------------------------------


# Public entry point


# ---------------------------------------------------------------------------








def _is_outside_workspace(tool_name: str, args: dict) -> bool:
    """Conservatively flag arguments that may reference an external path.

    This is a lexical diagnostic only: an absolute path can still be inside
    the active workspace, so approval decisions must not rely on it.  The
    workspace-aware tool handlers perform the authoritative boundary check.
    """
    # Check path-like arguments
    path_str = args.get("path") or args.get("file_path") or args.get("file")
    if path_str and isinstance(path_str, str):
        p = Path(path_str)
        # On Windows, Path.is_absolute() requires a drive letter, so
        # also check for Unix-style absolute paths (leading '/').
        if p.is_absolute() or path_str.startswith("/"):
            return True
        # Windows drive-letter absolute paths (C:\, D:\, etc.)
        if len(path_str) >= 2 and path_str[1] == ":":
            return True
        if ".." in path_str:
            return True

    # Check shell command argument for dangerous patterns
    cmd_str = args.get("command") or args.get("cmd")
    if cmd_str and isinstance(cmd_str, str):
        # Shell commands can access arbitrary paths — be conservative
        # when absolute paths or parent traversal appear in the command.
        import re
        # Absolute Unix paths like /etc, /home, etc.
        if re.search(r'(?<!\w)/[a-zA-Z]', cmd_str):
            return True
        # Windows absolute paths like C:\, D:\
        if re.search(r'[A-Za-z]:[\\/]', cmd_str):
            return True
        # Parent directory traversal
        if ".." in cmd_str:
            return True





    return False








def _run_agent_turn_unlocked(


    session_id: str,


    user_message: str,


    *,


    session_store: SessionStore,


    context_builder: ContextBuilder,


    tool_registry: ToolRegistry,


    llm_client: LLMClient,


    approval_handler: Callable | None = None,


    media: list[str] | None = None,


    skill_registry=None,


    skill_source: str = "",


    skill_name: str = "",


    auto_reason: str = "",


    compaction_worker=None,


    auto_mode: bool = False,


    unlimited_mode: bool = False,


    event_callback: Callable | None = None,


    cancel_event: threading.Event | None = None,


    input_event: str | None = None,

    _rollback_message_id: str | None = None,

    _rollback_checkpoint_id: str | None = None,


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


        auto_mode: if True, skip approval for write/shell tools while the


            workspace sandbox remains active.  Tool handlers still reject


            operations that escape the workspace.


        unlimited_mode: if True, the session is in unlimited mode. Shell


            tools always require explicit approval in this mode.


        event_callback: optional callback for turn events.


        cancel_event: optional ``threading.Event`` — when set, the loop


            exits at the next iteration boundary.


        input_event: optional internal event label attached to the input


            message.  Channels can use it to hide scheduler/system prompts


            from user-facing history without removing them from LLM context.





    Returns:


        The assistant's final reply text.


    """

    from claw.agent.health import LoopHealthMonitor
    from claw.agent.metrics import TurnMetrics, TurnMetricsAggregator

    metrics = TurnMetrics(session_id=session_id, turn_id=f"{session_id}:{time.time_ns()}")
    tool_outcomes: list[dict[str, Any]] = []
    tool_progress: dict[str, tuple[str, int]] = {}
    reply_finished = False

    def _record_outcome(tool_name: str, status: str, detail: str = "") -> None:
        """Keep a compact, non-sensitive record for deterministic summaries."""
        compact = " ".join(str(detail or "").split())
        if len(compact) > 180:
            compact = compact[:177] + "..."
        tool_outcomes.append({"tool": tool_name, "status": status, "detail": compact})

    def _record_serialized_outcome(tool_name: str, content: str) -> None:
        """Record a structured tool observation without trusting its shape."""
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            _record_outcome(tool_name, "error", "工具返回了无法解析的结果")
            return
        if not isinstance(payload, dict):
            _record_outcome(tool_name, "error", "工具返回了非对象结果")
            return
        ok = payload.get("ok") is True
        detail = payload.get("result", "")
        _record_outcome(tool_name, "ok" if ok else "error", str(detail))

    def _completion_brief(status: str, reason: str) -> str:
        labels = {
            "completed": "已完成",
            "partial": "部分完成",
            "cancelled": "已终止",
            "failed": "未完成",
        }
        successful = [item for item in tool_outcomes if item["status"] == "ok"]
        unsuccessful = [item for item in tool_outcomes if item["status"] != "ok"]
        lines = ["任务处理简报", f"- 状态：{labels.get(status, status)}"]
        if successful:
            names = list(dict.fromkeys(item["tool"] for item in successful))
            lines.append(f"- 已完成：{len(successful)} 次工具操作（{', '.join(names)}）")
            details = [item["detail"] for item in successful if item["detail"]]
            if details:
                lines.append(f"- 最近结果：{details[-1]}")
        if unsuccessful:
            names = list(dict.fromkeys(item["tool"] for item in unsuccessful))
            lines.append(f"- 未成功：{len(unsuccessful)} 次操作（{', '.join(names)}）")
            details = [item["detail"] for item in unsuccessful if item["detail"]]
            if details:
                lines.append(f"- 最近问题：{details[-1]}")
        if reason:
            lines.append(f"- 说明：{reason}")
        return "\n".join(lines)





    def _maybe_async_compact(_sess) -> None:


        """Background compaction is handled by the idle compaction worker.


        (only compact idle sessions, not every turn.)"""


        pass




    def _emit_event(event) -> None:


        """Emit a turn event without letting a UI callback break the turn."""


        if event_callback is None:


            return


        if not getattr(event, "timestamp", ""):


            event.timestamp = _now_iso()


        try:


            event_callback(event)


        except Exception:


            logger.exception("turn event callback 执行失败，已忽略")




    def _finish_reply(
        text: str | None,
        *,
        empty_reason: str = "",
        status: str = "completed",
    ) -> str:


        """Persist and emit exactly one non-empty assistant reply."""


        nonlocal reply_finished
        final_text = text if isinstance(text, str) else ""
        effective_status = status
        if effective_status == "completed" and not final_text.strip():
            effective_status = "partial" if tool_outcomes else "failed"

        # Successful answers stay clean. Abnormal termination remains visible
        # even when the model managed to produce some partial text.
        if effective_status != "completed":
            reason = empty_reason or "任务未正常完成，已保留本轮执行记录。"
            brief = _completion_brief(effective_status, reason)
            final_text = f"{final_text.rstrip()}\n\n{brief}" if final_text.strip() else brief

        if reply_finished:
            logger.warning("忽略重复的 turn 终止请求: session=%s", session_id)
            return final_text
        reply_finished = True


        session.append_message("assistant", final_text)


        _save_safe(session_store, session)


        _maybe_async_compact(session)


        _emit_event(FinalEvent(content=final_text))

        metric_status = "ok" if effective_status == "completed" else effective_status
        metrics.finalize(
            status=metric_status,
            error="" if effective_status == "completed" else empty_reason,
        )
        with _metrics_lock:
            aggregator = _session_aggregators.setdefault(session_id, TurnMetricsAggregator())
            monitor = _session_health_monitors.setdefault(session_id, LoopHealthMonitor())
            while len(_session_aggregators) > _MAX_METRIC_SESSIONS:
                oldest_session = next(iter(_session_aggregators))
                if oldest_session == session_id and len(_session_aggregators) > 1:
                    oldest_session = next(
                        key for key in _session_aggregators if key != session_id
                    )
                _session_aggregators.pop(oldest_session, None)
                _session_health_monitors.pop(oldest_session, None)
        aggregator.add(metrics)
        monitor.record_turn(metrics)
        alerts = monitor.check_health()
        if alerts:
            logger.warning(
                "Agent loop health alert session=%s: %s",
                session_id,
                "; ".join(f"{alert.category}:{alert.level}" for alert in alerts),
            )


        return final_text





    def _needs_approval(tool_name: str) -> bool:


        t = tool_registry.get_tool(tool_name)


        return t is not None and t.safety_level in _APPROVAL_REQUIRED_LEVELS





    def _is_skill_select(tool_name: str) -> bool:


        t = tool_registry.get_tool(tool_name)


        return t is not None and t.safety_level == _SKILL_SELECT_LEVEL





    def _is_shell_tool(tool_name: str) -> bool:


        """Check if a tool is a shell-type tool (safety_level == 'shell')."""


        t = tool_registry.get_tool(tool_name)


        return t is not None and t.safety_level == "shell"





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


        session.append_message(
            "user",
            injected,
            media=media,
            injected_event=input_event,
            message_id=_rollback_message_id or f"msg_{uuid.uuid4().hex}",
            rollback_checkpoint_id=_rollback_checkpoint_id,
        )


        _record_skill_usage(


            session, skill_name, user_message, "explicit", ""


        )


    else:


        session.append_message(
            "user",
            user_message,
            media=media,
            injected_event=input_event,
            message_id=_rollback_message_id or f"msg_{uuid.uuid4().hex}",
            rollback_checkpoint_id=_rollback_checkpoint_id,
        )





    _save_safe(session_store, session)





    # Track whether we've already handled skill selection in this turn


    skill_already_injected_for_turn = skill_source == "explicit"





    # -- 2. Think → Act → Observe loop ---------------------------------------


    turn_count = 0


    tool_calls_used = 0


    tool_limit_reached = False


    while True:


        # Check for cancellation at the top of each iteration


        if cancel_event is not None and cancel_event.is_set():


            logger.info("Agent turn cancelled by user at iteration %d", turn_count)


            return _finish_reply(
                None,
                empty_reason="用户已终止本轮任务；已完成的操作和结果仍然保留。",
                status="cancelled",
            )





        turn_count += 1


        if turn_count > _MAX_AGENT_ITERATIONS:


            print(f"[agent] 已达到最大迭代次数 {_MAX_AGENT_ITERATIONS}，强制终止循环")


            metrics.record_max_iterations()
            return _finish_reply(
                None,
                empty_reason=(
                    f"已达到 {_MAX_AGENT_ITERATIONS} 次迭代上限；"
                    "为防止循环失控已安全停止，可拆分任务后继续。"
                ),
                status="partial" if tool_outcomes else "failed",
            )





        metrics.record_iteration()
        _emit_event(ThinkingEvent(iteration=turn_count))


        llm_call_started = False
        try:


            messages = context_builder.build_messages(


                session,


                tool_registry=None if tool_limit_reached else tool_registry,


                include_tool_instructions=not tool_limit_reached,


            )


            tool_defs = [] if tool_limit_reached else context_builder.get_tool_definitions(tool_registry)





            metrics.start_llm_call()
            llm_call_started = True
            response = llm_client.chat_with_tools(messages, tool_defs)
            if response is None or not hasattr(response, "is_final") or not hasattr(response, "is_tool_call"):
                raise TypeError("模型客户端返回了无效的 AgentResponse")
            metrics.end_llm_call(
                ok=True,
                truncated=getattr(response, "finish_reason", None) == "length",
            )


        except Exception:

            if llm_call_started:
                metrics.end_llm_call(ok=False)


            logger.exception("Agent 在第 %d 轮调用模型失败", turn_count)


            _emit_event(ErrorEvent(error="模型调用失败，Agent 已安全结束本轮任务。"))


            return _finish_reply(
                None,
                empty_reason=(
                    "工具调用已执行，但模型服务在整理最终回复时发生异常；"
                    "已保留当前工具结果，请稍后重试。"
                    if tool_outcomes
                    else "模型服务暂时不可用，本轮未能开始处理，请稍后重试。"
                ),
                status="partial" if tool_outcomes else "failed",
            )





        # -- Final answer ----------------------------------------------------


        if response.is_final and response.final is not None:


            final_text = response.final

            if getattr(response, "finish_reason", None) == "length":
                if final_text.strip():
                    final_text = (
                        final_text.rstrip()
                        + "\n\n> 回复达到模型输出长度限制，以上内容可能不完整；可让我继续。"
                    )
                return _finish_reply(
                    final_text,
                    empty_reason="模型输出达到长度限制且未返回正文；已保留本轮进度。",
                    status="partial",
                )


            return _finish_reply(


                final_text,


                empty_reason=(


                    "工具调用已经完成，但模型没有生成最终回复。"


                    "工具结果已保留，请重试最后一步。"


                    if tool_calls_used


                    else "模型没有返回有效内容，请重试。"


                ),
                status=(
                    "completed"
                    if final_text.strip()
                    else ("partial" if tool_outcomes else "failed")
                ),


            )





        # -- Tool call(s) ----------------------------------------------------


        if response.is_tool_call:

            if tool_limit_reached:
                return _finish_reply(
                    None,
                    empty_reason=(
                        "模型在工具调用已关闭后仍请求工具；为避免空转已安全停止。"
                    ),
                    status="partial" if tool_outcomes else "failed",
                )

            native_tool_calls = []
            used_call_ids = {
                str(call.get("id"))
                for message in session.messages
                for call in (message.tool_calls or [])
                if isinstance(call, dict) and call.get("id")
            }
            for index, tc in enumerate(response.tool_calls, start=1):
                call_id = str(getattr(tc, "call_id", "") or "").strip()
                if not call_id or call_id in used_call_ids:
                    base_id = f"tool_{turn_count}_{index}"
                    call_id = base_id
                    suffix = 2
                    while call_id in used_call_ids:
                        call_id = f"{base_id}_{suffix}"
                        suffix += 1
                used_call_ids.add(call_id)
                tc.call_id = call_id
                native_tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.args, ensure_ascii=False),
                        },
                    }
                )
            session.append_message("assistant", "", tool_calls=native_tool_calls)

            def append_tool_result(tc, content: str) -> None:
                session.append_message(
                    "tool",
                    content,
                    tool_call_id=tc.call_id,
                    name=tc.name,
                )


            pending_skill_injections: list[str] = []
            batch_cancelled = False

            for tc in response.tool_calls:

                if cancel_event is not None and cancel_event.is_set():
                    batch_cancelled = True
                    cancel_message = "用户已终止本轮任务，工具未执行。"
                    append_tool_result(
                        tc,
                        json.dumps(
                            {"tool": tc.name, "ok": False, "result": cancel_message},
                            ensure_ascii=False,
                        ),
                    )
                    _record_outcome(tc.name, "blocked", cancel_message)
                    continue

                if tool_limit_reached or tool_calls_used >= _MAX_TOOL_CALLS_PER_TURN:


                    tool_limit_reached = True


                    limit_message = (


                        f"本轮工具调用已达到上限（{_MAX_TOOL_CALLS_PER_TURN}）。"


                        "请基于已有结果直接总结并回复用户。"


                    )


                    append_tool_result(
                        tc,
                        json.dumps(


                            {"tool": tc.name, "ok": False, "result": limit_message},


                            ensure_ascii=False,


                        ),


                    )

                    _record_outcome(tc.name, "blocked", limit_message)


                    logger.warning("单轮工具调用达到上限: %d", _MAX_TOOL_CALLS_PER_TURN)


                    continue


                tool_calls_used += 1


                args_json = json.dumps(tc.args, ensure_ascii=False)

                call_signature = f"{tc.name}:{json.dumps(tc.args, sort_keys=True, ensure_ascii=False)}"


                print(f"[tool_call #{turn_count}] {tc.name} {args_json}")





                # ---- use_skill (second call already handled) -------------------


                if _is_skill_select(tc.name) and skill_already_injected_for_turn:


                    msg = (


                        f"skill \"{skill_name or '?'}\" 已在本轮中加载，"


                        f"无需重复调用 use_skill，请继续执行当前任务。"


                    )


                    append_tool_result(
                        tc,
                        json.dumps(


                            {"tool": "use_skill", "ok": True, "result": msg},


                            ensure_ascii=False,


                        ),


                    )


                    print(f"[tool_result] use_skill: {msg}")
                    _record_outcome("use_skill", "ok", msg)


                    continue





                # ---- use_skill (skill_select) -------------------------------


                if _is_skill_select(tc.name) and not skill_already_injected_for_turn:


                    result, injected = _handle_skill_select(


                        tc.args,


                        session,


                        session_store,


                        context_builder,


                        approval_handler,


                        session_id,


                        skill_registry,


                    )


                    append_tool_result(tc, result)
                    _record_serialized_outcome("use_skill", result)
                    if injected is not None:
                        pending_skill_injections.append(injected)
                        skill_already_injected_for_turn = True

                    continue





                # ---- Approval gate for write / shell tools ------------------


                needs_approval = _needs_approval(tc.name)


                # A channel without an interactive approval mechanism must
                # always fail closed. Otherwise remote channels could bypass
                # the approval gate simply by omitting the callback.
                if needs_approval and approval_handler is None:


                    result_content = json.dumps(


                        {


                            "tool": tc.name,


                            "ok": False,


                            "result": (


                                "写入、删除和 shell 操作必须经过用户审批。"


                                "当前通道不支持交互式审批，操作已拒绝；请改用 CLI 或 WebUI。"


                            ),


                        },


                        ensure_ascii=False,


                    )


                    append_tool_result(tc, result_content)

                    _record_outcome(tc.name, "rejected", "当前通道不支持交互式审批")


                    print(f"[tool_result] {tc.name}: 已拒绝 — 当前通道不支持显式审批")


                    continue


                if needs_approval and approval_handler is not None:


                    from claw.approval.manager import ApprovalStatus





                    # In sandboxed mode, file and shell handlers enforce the


                    # workspace boundary themselves.  Do not infer an escape


                    # merely because the model used an absolute path: it may


                    # still point inside the workspace, and the old heuristic


                    # caused intermittent approval prompts in AUTO mode.


                    # In unlimited mode, every write/shell tool ALWAYS


                    #    requires approval because relative paths and shell


                    #    commands can also resolve outside the workspace.


                    force_approval = unlimited_mode





                    if force_approval:


                        if unlimited_mode:


                            logger.warning(


                                "write/shell tool %s in unlimited mode, "


                                "forcing approval",


                                tc.name,


                            )





                    if auto_mode and not force_approval:


                        # AUTO mode: skip approval, tool operates freely


                        # within workspace.  Workspace boundary is still


                        # enforced by the tool handlers.


                        print(f"[auto] 自动批准 {tc.name}（AUTO 模式）")


                    else:


                        req = _make_approval_request(session_id, tc.name, tc.args)


                        try:
                            decided = approval_handler(req)
                        except Exception as exc:
                            logger.exception("工具 %s 的审批回调执行失败", tc.name)
                            _emit_event(ErrorEvent(error=f"{tc.name} 的审批流程异常，操作已安全拒绝。"))
                            decided = None
                            approval_error = str(exc)
                        else:
                            approval_error = ""





                        decision_status = getattr(decided, "status", None)
                        if decision_status != ApprovalStatus.APPROVED.value:


                            reason = (


                                getattr(decided, "reject_reason", "")


                                if decided and getattr(decided, "reject_reason", "")


                                else approval_error or "审批未明确通过，操作已安全拒绝"


                            )


                            # Track consecutive rejections to break retry loops


                            rejection_key = f"{tc.name}:{json.dumps(tc.args, sort_keys=True, ensure_ascii=False)}"


                            rejection_count = _rejection_tracker.get(rejection_key, 0) + 1


                            _rejection_tracker[rejection_key] = rejection_count





                            if rejection_count >= _MAX_REJECTIONS_PER_OPERATION:


                                result_content = json.dumps({


                                    "tool": tc.name,


                                    "ok": False,


                                    "result": (


                                        f"工具调用已被连续拒绝 {rejection_count} 次，操作已中止。"


                                        f"请检查工具参数是否正确，或尝试其他方式完成此任务。"


                                    ),


                                }, ensure_ascii=False)


                            else:


                                result_content = json.dumps({


                                    "tool": tc.name,


                                    "ok": False,


                                    "result": (


                                        f"工具调用第 {rejection_count} 次被拒绝，原因：{reason}。"


                                        f"请检查工具参数是否正确，或尝试其他方式完成此任务。"


                                        f"如反复被拒，建议与审批者沟通确认。"


                                    ),


                                }, ensure_ascii=False)


                            append_tool_result(tc, result_content)

                            _record_outcome(tc.name, "rejected", reason)


                            print(
                                f"[tool_result] {tc.name}: 已拒绝 "
                                f"({rejection_count}/{_MAX_REJECTIONS_PER_OPERATION}) — {reason}"
                            )


                            continue





                # ---- Execute the tool ---------------------------------------


                call_id = tc.call_id


                _emit_event(


                    ToolCallStartEvent(


                        call_id=call_id,


                        tool_name=tc.name,


                        args=dict(tc.args),


                        iteration=turn_count,


                    )


                )


                tool_started_at = time.perf_counter()

                metrics.start_tool_call()

                try:
                    result = tool_registry.execute_by_name(tc.name, tc.args)
                except Exception as exc:
                    logger.exception("工具注册表执行 %s 时发生异常", tc.name)
                    result = ToolResult(
                        ok=False,
                        error=f"工具执行框架发生异常，操作已安全中止：{exc}",
                    )
                    _emit_event(ErrorEvent(error=f"{tc.name} 执行异常，已转为失败结果。"))

                if not all(hasattr(result, attr) for attr in ("ok", "content", "error")):
                    logger.error("工具注册表为 %s 返回了无效类型: %s", tc.name, type(result).__name__)
                    result = ToolResult(ok=False, error="工具执行框架返回了无效结果")

                metrics.end_tool_call(ok=result.ok)





                if result.ok:


                    result_content = result.content or "(空结果)"


                    print(f"[tool_result] {tc.name}: 成功")

                    _record_outcome(tc.name, "ok", result_content)


                else:


                    result_content = json.dumps(


                        {"tool": tc.name, "ok": False, "result": f"错误: {result.error}"},


                        ensure_ascii=False,


                    )


                    print(f"[tool_result] {tc.name}: 失败 — {result.error}")

                    _record_outcome(tc.name, "error", result.error or "未知错误")





                tool_msg_content = result.content if (result.ok and result.content) else result_content

                # Polling is legitimate while results change. Treat only an
                # unchanged tool+args+result sequence as a stuck loop.
                result_fingerprint = f"{result.ok}:{tool_msg_content}"
                previous_fingerprint, stagnant_count = tool_progress.get(
                    call_signature, ("", 0)
                )
                stagnant_count = stagnant_count + 1 if previous_fingerprint == result_fingerprint else 1
                tool_progress[call_signature] = (result_fingerprint, stagnant_count)
                if stagnant_count >= _MAX_IDENTICAL_TOOL_CALLS:
                    tool_limit_reached = True
                    repeat_message = (
                        f"检测到相同工具和相同结果连续出现 {stagnant_count} 次，"
                        "已停止继续调用工具并进入结果总结。"
                    )
                    tool_msg_content = f"{tool_msg_content}\n\n[loop_guard] {repeat_message}"
                    _record_outcome(tc.name, "blocked", repeat_message)
                    logger.warning("检测到无进展的重复工具调用: %s", call_signature)


                append_tool_result(tc, tool_msg_content)


                _emit_event(


                    ToolCallEndEvent(


                        call_id=call_id,


                        tool_name=tc.name,


                        ok=result.ok,


                        result=result.content if result.ok else None,


                        error=result.error if not result.ok else None,


                        duration_ms=round(


                            (time.perf_counter() - tool_started_at) * 1000,


                            2,


                        ),


                    )


                )





                # If this was an overwrite_file and skill was active,


                # record the output path


                if tc.name == "overwrite_file" and skill_name:


                    _update_skill_output(session, tc.args.get("path", ""))





            for injected in pending_skill_injections:
                session.append_message("user", injected)

            _save_safe(session_store, session)

            if batch_cancelled:
                return _finish_reply(
                    None,
                    empty_reason="用户已终止本轮任务；已完成的操作和结果仍然保留。",
                    status="cancelled",
                )

            continue





        # -- Neither final nor tool_call ------------------------------------


        logger.warning("LLM 响应既不是 final 也不包含 tool_call")


        _emit_event(ErrorEvent(error="模型返回了无法识别的空响应。"))


        return _finish_reply(


            None,


            empty_reason="模型没有返回可处理的内容，本轮任务已安全结束。请重试。",
            status="partial" if tool_outcomes else "failed",


        )








# ---------------------------------------------------------------------------


def run_agent_turn(
    session_id: str,
    user_message: str,
    *,
    rollback_manager=None,
    **kwargs,
) -> str:
    """Run a turn, capturing a workspace checkpoint before user input.

    The workspace lock spans the complete turn so shell changes from sessions
    sharing one workspace cannot interleave with a checkpoint or restore.
    """
    if rollback_manager is None:
        return _run_agent_turn_unlocked(session_id, user_message, **kwargs)

    session_store = kwargs.get("session_store")
    if session_store is None:
        raise TypeError("session_store is required")
    with rollback_manager.turn_guard(session_id):
        session = session_store.get(session_id)
        message_id = f"msg_{uuid.uuid4().hex}"
        checkpoint_id = rollback_manager.create_turn_checkpoint(
            session_id,
            session,
            message_id=message_id,
            message_preview=user_message,
            partial=bool(kwargs.get("unlimited_mode", False)),
        )
        return _run_agent_turn_unlocked(
            session_id,
            user_message,
            _rollback_message_id=message_id,
            _rollback_checkpoint_id=checkpoint_id,
            **kwargs,
        )


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


) -> tuple[str, str | None]:


    """Handle a ``use_skill`` tool call.





    1. Resolve the skill name from args.


    2. If approval_handler is provided, create a skill approval and wait.


    3. If approved (or no handler), inject the skill content.


    4. Record skill usage.





    Returns:


        A tool-observation JSON string and optional skill-injection message.


    """


    from claw.approval.manager import ApprovalRequest, ApprovalStatus





    skill_name = args.get("skill_name", "").strip()


    auto_reason = args.get("reason", "").strip()





    if not skill_name:


        return json.dumps(


            {"tool": "use_skill", "ok": False, "result": "错误: 未提供有效的 skill 名称"},


            ensure_ascii=False,


        ), None





    # Verify skill exists


    if skill_registry is None or skill_registry.get_skill(skill_name) is None:


        return json.dumps(


            {"tool": "use_skill", "ok": False, "result": f"错误: 未找到名为 skill \"{skill_name}\""},


            ensure_ascii=False,


        ), None





    # -- Skill approval ------------------------------------------------------


    if approval_handler is not None:


        req = ApprovalRequest(


            session_id=session_id,


            tool_name="use_skill",


            tool_args={


                "skill_name": skill_name,


                "reason": auto_reason or "自动审批：未找到指定的 skill，使用默认理由",


            },


        )


        try:
            decided = approval_handler(req)
        except Exception as exc:
            logger.exception("skill %s 的审批回调执行失败", skill_name)
            return json.dumps(
                {
                    "tool": "use_skill",
                    "ok": False,
                    "result": f"skill 审批流程异常，已安全拒绝: {exc}",
                },
                ensure_ascii=False,
            ), None





        if getattr(decided, "status", None) != ApprovalStatus.APPROVED.value:


            reason = (


                getattr(decided, "reject_reason", "")


                if decided and getattr(decided, "reject_reason", "")


                else "skill 审批未明确通过，已安全拒绝"


            )


            print(f"[skill] {skill_name}: 加载被拒绝 — {reason}")


            return json.dumps(


                {"tool": "use_skill", "ok": False, "result": f"skill 加载被拒绝: {reason}"},


                ensure_ascii=False,


            ), None





    # -- Inject skill content ------------------------------------------------


    print(f"[skill] {skill_name}: 已批准，正在注入 skill 内容")





    # Get the original user task (the first user message of this turn)


    user_task = ""


    for m in reversed(session.messages):


        if m.role == "user":


            user_task = m.content


            break





    injected = context_builder.build_skill_injection_message(skill_name, user_task)


    # The caller appends the matching tool result before this injected user
    # message so the native function-calling sequence remains valid.





    _record_skill_usage(session, skill_name, user_task, "auto", auto_reason)


    # The caller saves after appending both protocol messages.





    return json.dumps(
        {"tool": "use_skill", "ok": True, "result": f"skill \"{skill_name}\" 已加载"},
        ensure_ascii=False,
    ), injected








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


    except Exception as exc:


        print(f"[警告] session 保存失败: {exc}")


        logger.exception("session 保存失败: %s", getattr(session, "session_id", "?"))
