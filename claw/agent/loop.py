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


from claw.tools.base import ToolRegistry





logger = logging.getLogger(__name__)





_APPROVAL_REQUIRED_LEVELS = {"write", "shell"}


_SKILL_SELECT_LEVEL = "skill_select"


_MAX_AGENT_ITERATIONS = int(os.getenv("CLAW_MAX_AGENT_ITERATIONS", "15"))
_MAX_TOOL_CALLS_PER_TURN = max(1, int(os.getenv("CLAW_MAX_TOOL_CALLS_PER_TURN", "20")))


_MAX_REJECTIONS_PER_OPERATION = 3  # stop LLM from retrying the same rejected operation








def _now_iso() -> str:


    return datetime.now(timezone.utc).isoformat(timespec="seconds")








# Per-session metrics aggregators (roll up turn stats for diagnostics)


_session_aggregators: dict[str, Any] = {}








def get_session_metrics_summary(session_id: str) -> dict[str, Any] | None:


    """Return the aggregated metrics summary for *session_id*, or None."""


    from claw.agent.metrics import TurnMetricsAggregator


    agg = _session_aggregators.get(session_id)


    return agg.summary() if agg else None








# ---------------------------------------------------------------------------


# Public entry point


# ---------------------------------------------------------------------------








def _is_outside_workspace(tool_name: str, args: dict) -> bool:
    """Check if a tool call targets a path outside the workspace.

    In unlimited mode, operations outside the workspace always
    require explicit approval — even when auto_mode is on.
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


    unlimited_mode: bool = False,


    event_callback: Callable | None = None,


    cancel_event: threading.Event | None = None,


    input_event: str | None = None,


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


        auto_mode: if True, skip approval for write/shell tools (within


            workspace only; outside-workspace ops still require approval).


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





    def _maybe_async_compact(_sess) -> None:


        """Background compaction is handled by the idle compaction worker.


        (nanobot-style: only compact idle sessions, not every turn.)"""


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




    def _finish_reply(text: str | None, *, empty_reason: str = "") -> str:


        """Persist and emit exactly one non-empty assistant reply."""


        final_text = text if isinstance(text, str) else ""


        if not final_text.strip():


            final_text = empty_reason or (


                "本轮处理已经结束，但模型没有生成有效回复。"


                "请重试，或将任务拆分成更小的步骤。"


            )


        session.append_message("assistant", final_text)


        _save_safe(session_store, session)


        _maybe_async_compact(session)


        _emit_event(FinalEvent(content=final_text))


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


        session.append_message("user", injected, injected_event=input_event)


        _record_skill_usage(


            session, skill_name, user_message, "explicit", ""


        )


    else:


        session.append_message("user", user_message, injected_event=input_event)





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


            cancel_msg = "任务已被用户终止。"


            return _finish_reply(cancel_msg)





        turn_count += 1


        if turn_count > _MAX_AGENT_ITERATIONS:


            print(f"[agent] 已达到最大迭代次数 {_MAX_AGENT_ITERATIONS}，强制终止循环")


            final_text = (


                "已达到最大迭代次数限制，代理循环已被强制终止。"


                "请检查任务是否过于复杂，可尝试拆分为更小的子任务后重新运行。"


            )


            return _finish_reply(final_text)





        _emit_event(ThinkingEvent(iteration=turn_count))


        try:


            messages = context_builder.build_messages(


                session,


                tool_registry=None if tool_limit_reached else tool_registry,


                include_tool_instructions=not tool_limit_reached,


            )


            tool_defs = [] if tool_limit_reached else context_builder.get_tool_definitions(tool_registry)





            response = llm_client.chat_with_tools(messages, tool_defs)


        except Exception:


            logger.exception("Agent 在第 %d 轮调用模型失败", turn_count)


            _emit_event(ErrorEvent(error="模型调用失败，Agent 已安全结束本轮任务。"))


            if tool_calls_used:


                message = (


                    "工具调用已执行，但在整理最终结果时模型服务发生异常。"


                    "已保留当前工具结果，请稍后重试。"


                )


            else:


                message = "模型服务暂时不可用，本轮未能完成处理。请稍后重试。"


            return _finish_reply(message)





        # -- Final answer ----------------------------------------------------


        if response.is_final and response.final is not None:


            final_text = response.final


            return _finish_reply(


                final_text,


                empty_reason=(


                    "工具调用已经完成，但模型没有生成最终回复。"


                    "工具结果已保留，请重试最后一步。"


                    if tool_calls_used


                    else "模型没有返回有效内容，请重试。"


                ),


            )





        # -- Tool call(s) ----------------------------------------------------


        if response.is_tool_call:


            for tc in response.tool_calls:


                if tool_calls_used >= _MAX_TOOL_CALLS_PER_TURN:


                    tool_limit_reached = True


                    limit_message = (


                        f"本轮工具调用已达到上限（{_MAX_TOOL_CALLS_PER_TURN}）。"


                        "请基于已有结果直接总结并回复用户。"


                    )


                    session.append_message(


                        "tool",


                        json.dumps(


                            {"tool": tc.name, "ok": False, "result": limit_message},


                            ensure_ascii=False,


                        ),


                    )


                    logger.warning("单轮工具调用达到上限: %d", _MAX_TOOL_CALLS_PER_TURN)


                    continue


                tool_calls_used += 1


                args_json = json.dumps(tc.args, ensure_ascii=False)


                print(f"[tool_call #{turn_count}] {tc.name} {args_json}")





                # ---- use_skill (second call already handled) -------------------


                if _is_skill_select(tc.name) and skill_already_injected_for_turn:


                    msg = (


                        f"skill \"{skill_name or '?'}\" 已在本轮中加载，"


                        f"无需重复调用 use_skill，请继续执行当前任务。"


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


                needs_approval = _needs_approval(tc.name)


                # UNLIMITED removes the workspace sandbox.  Every mutating or
                # shell operation therefore requires an explicit user decision,
                # even when AUTO is enabled.  A channel without an interactive
                # approval mechanism must fail closed instead of executing.
                if needs_approval and unlimited_mode and approval_handler is None:


                    result_content = json.dumps(


                        {


                            "tool": tc.name,


                            "ok": False,


                            "result": (


                                "UNLIMITED 模式下的写入、删除和 shell 操作必须经过用户审批。"


                                "当前通道不支持交互式审批，操作已拒绝；请改用 CLI 或 WebUI。"


                            ),


                        },


                        ensure_ascii=False,


                    )


                    session.append_message("tool", result_content)


                    print(f"[tool_result] {tc.name}: 已拒绝 — UNLIMITED 模式需要显式审批")


                    continue


                if needs_approval and approval_handler is not None:


                    from claw.approval.manager import ApprovalStatus





                    # Security: determine if this operation is potentially


                    # dangerous and should bypass auto_mode approval.


                    # 1. Operations targeting paths outside the workspace


                    #    ALWAYS require explicit approval.


                    # 2. In unlimited mode, every write/shell tool ALWAYS


                    #    requires approval because relative paths and shell


                    #    commands can also resolve outside the workspace.


                    outside_ws = _is_outside_workspace(tc.name, tc.args)


                    force_approval = outside_ws or unlimited_mode





                    if force_approval:


                        if outside_ws:


                            logger.warning(


                                "tool %s targets path outside workspace, "


                                "forcing approval even in AUTO mode",


                                tc.name,


                            )


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


                        decided = approval_handler(req)





                        if decided is None or decided.status == ApprovalStatus.REJECTED.value:


                            reason = (


                                decided.reject_reason


                                if decided and decided.reject_reason


                                else "操作被拒绝，未提供具体原因"


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


                            session.append_message("tool", result_content)


                            print(f"[tool_result] {tc.name}: 已拒绝 ({rejection_count}/3) — {reason}")


                            continue





                # ---- Execute the tool ---------------------------------------


                call_id = getattr(tc, "call_id", "") or f"tool_{turn_count}_{tool_calls_used}"


                _emit_event(


                    ToolCallStartEvent(


                        call_id=call_id,


                        tool_name=tc.name,


                        args=dict(tc.args),


                        iteration=turn_count,


                    )


                )


                tool_started_at = time.perf_counter()


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





            _save_safe(session_store, session)


            continue





        # -- Neither final nor tool_call ------------------------------------


        logger.warning("LLM 响应既不是 final 也不包含 tool_call")


        _emit_event(ErrorEvent(error="模型返回了无法识别的空响应。"))


        return _finish_reply(


            None,


            empty_reason="模型没有返回可处理的内容，本轮任务已安全结束。请重试。",


        )








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


            {"tool": "use_skill", "ok": False, "result": "错误: 未提供有效的 skill 名称"},


            ensure_ascii=False,


        )





    # Verify skill exists


    if skill_registry is None or skill_registry.get_skill(skill_name) is None:


        return json.dumps(


            {"tool": "use_skill", "ok": False, "result": f"错误: 未找到名为 skill \"{skill_name}\""},


            ensure_ascii=False,


        )





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


        decided = approval_handler(req)





        if decided is None or decided.status == ApprovalStatus.REJECTED.value:


            reason = (


                decided.reject_reason


                if decided and decided.reject_reason


                else "用户拒绝加载该 skill，未提供具体原因"


            )


            print(f"[skill] {skill_name}: 加载被拒绝 — {reason}")


            return json.dumps(


                {"tool": "use_skill", "ok": False, "result": f"skill 加载被拒绝: {reason}"},


                ensure_ascii=False,


            )





    # -- Inject skill content ------------------------------------------------


    print(f"[skill] {skill_name}: 已批准，正在注入 skill 内容")





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


    except Exception as exc:


        print(f"[警告] session 保存失败: {exc}")


        logger.exception("session 保存失败: %s", getattr(session, "session_id", "?"))
