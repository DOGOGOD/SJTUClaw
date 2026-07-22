"""Black-box/grey-box acceptance tasks for the current SJTUClaw runtime.

The runner must be launched with SJTUCLAW_DATA_DIR pointing at an isolated
directory.  It drives the public Gateway API with a deterministic local model
substitute so project prompts and materials never leave the machine.  Direct
SessionStore access is used only to seed a long transcript for compaction.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "claw_evaluation_workspace"
RESULT_PATH = ROOT / "evaluation" / "claw_acceptance_results.json"


def require_isolation() -> Path:
    raw = os.environ.get("SJTUCLAW_DATA_DIR", "").strip()
    if not raw:
        raise SystemExit("Refusing to run without SJTUCLAW_DATA_DIR")
    target = Path(raw).resolve()
    if target == (ROOT / "data").resolve():
        raise SystemExit("Refusing to use the normal project data directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


DATA_DIR = require_isolation()

# Import only after the isolated data directory is established.
from claw.gateway import server  # noqa: E402
from claw.gateway.server import app  # noqa: E402
from claw.llm.protocol import AgentResponse, ToolCallRequest  # noqa: E402
from claw.session.store import SessionStore  # noqa: E402


results: list[dict[str, Any]] = []


class LocalScriptedLLM:
    """Deterministic local model substitute that drives the real agent loop."""

    def __init__(self) -> None:
        self.steps: dict[str, int] = {}
        self.config = server._llm_client.config

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def _prompt(self, messages: list[dict[str, Any]]) -> str:
        users = [self._text(m.get("content", "")) for m in messages if m.get("role") == "user"]
        return users[-1] if users else ""

    def _call(self, name: str, args: dict[str, Any], suffix: str) -> AgentResponse:
        return AgentResponse(
            tool_calls=[ToolCallRequest(name=name, args=args, call_id=f"local-{suffix}")],
            finish_reason="tool_calls",
        )

    def chat_with_tools(self, messages, _tool_defs, **_kwargs):
        prompt = self._prompt(messages)
        blob = json.dumps(messages, ensure_ascii=False)
        step = self.steps.get(prompt, 0)
        self.steps[prompt] = step + 1

        if "CLAW-BASE-OK" in prompt:
            return AgentResponse(final="CLAW-BASE-OK", finish_reason="stop")
        if "记住本 session 代号 ORBIT-731" in prompt:
            return AgentResponse(final="已记录", finish_reason="stop")
        if "代号后三位按规则" in prompt:
            answer = "1462" if "ORBIT-731" in blob else "MISSING-CONTEXT"
            return AgentResponse(final=answer, finish_reason="stop")
        if "是否有人告诉过你 ORBIT" in prompt:
            answer = "UNKNOWN" if "ORBIT-731" not in blob else "LEAKED"
            return AgentResponse(final=answer, finish_reason="stop")
        if "根据长期记忆" in prompt:
            answer = (
                "结构偏好：结论先行；验收标记：琥珀-417。"
                if "琥珀-417" in blob and "结论先行" in blob
                else "MISSING-MEMORY"
            )
            return AgentResponse(final=answer, finish_reason="stop")
        if "项目代号是什么" in prompt:
            answer = "NEBULA-2049" if "NEBULA-2049" in blob else "MISSING-SUMMARY"
            return AgentResponse(final=answer, finish_reason="stop")
        if "推荐哪个 variant" in prompt:
            if step == 0:
                return self._call("list_dir", {"path": "materials"}, "list")
            if step == 1:
                return AgentResponse(
                    tool_calls=[
                        ToolCallRequest(
                            name="read_file",
                            args={"path": "materials/lecture_notes.md"},
                            call_id="local-notes",
                        ),
                        ToolCallRequest(
                            name="read_file",
                            args={"path": "materials/experiment_results.csv"},
                            call_id="local-csv",
                        ),
                    ],
                    finish_reason="tool_calls",
                )
            return AgentResponse(
                final=(
                    "若优先时延，推荐 variant B：82 ms，成功率 0.991；若可靠性优先则 C 的 "
                    "0.998 更高。三节点发生网络分区时，拥有两个节点的多数派一侧能选出 "
                    "Leader 并继续提交，单节点少数派不能形成多数确认。"
                ),
                finish_reason="stop",
            )
        if "定时验收" in prompt:
            return AgentResponse(final="CRON-EXECUTED-2026", finish_reason="stop")
        if "请完成课程报告草稿任务" in prompt:
            if step == 0:
                return self._call("skills_list", {}, "skills")
            if step == 1:
                return self._call("skill_view", {"name": "course-report"}, "skill-main")
            if step == 2:
                return self._call(
                    "skill_view",
                    {"name": "course-report", "file_path": "references/checklist.md"},
                    "skill-checklist",
                )
            if step == 3:
                return AgentResponse(
                    tool_calls=[
                        ToolCallRequest(
                            name="read_file",
                            args={"path": "materials/lecture_notes.md"},
                            call_id="local-report-notes",
                        ),
                        ToolCallRequest(
                            name="read_file",
                            args={"path": "materials/experiment_results.csv"},
                            call_id="local-report-csv",
                        ),
                    ],
                    finish_reason="tool_calls",
                )
            if step == 4:
                report = """# Raft 的可理解性与工程权衡

## 摘要

Raft 将共识问题拆分为领导者选举、日志复制与安全性约束，使工程人员能够沿清晰状态机理解系统行为。本文结合三节点网络分区案例和三组实验数据，讨论这种可理解性如何降低实现与运维风险，同时说明安全性、时延和可用性之间不能被简单合并的权衡。

## 一、从角色与任期理解共识

Raft 的节点在 Follower、Candidate 与 Leader 三种角色间转换，并用单调递增的任期组织时间。Follower 超时后成为 Candidate，向其他节点请求投票；获得多数票后成为 Leader。一个任期最多产生一个 Leader，这条选举安全性让故障分析有明确边界。日志匹配性质进一步保证：若两份日志在相同索引处拥有相同任期，则该位置之前的日志也一致。因而实现者可以分别检查选举、复制和提交，而不必同时推理所有消息交错。

## 二、三节点网络分区案例

设集群由 A、B、C 三个节点组成，A 原为 Leader。网络分区后 A 单独位于一侧，B 与 C 位于另一侧。A 即使继续接收客户端请求，也无法获得两个节点的多数确认，所以不能提交新的日志项。B、C 一侧拥有多数派，可以在超时后选举新 Leader，并在二者复制成功后继续提交。分区恢复时，A 会依据更高任期退回 Follower，并接受新 Leader 的日志。这个案例说明，可用性属于多数派一侧，而日志安全性优先于少数派的短期响应能力。

## 三、实验数据与工程选择

实验中 variant A 的时延为 120 ms、成功率为 0.995，可作为基线；variant B 的时延最低，为 82 ms，但成功率为 0.991；variant C 的时延为 95 ms，成功率最高，达到 0.998。若交互系统首先追求响应速度，B 是合理选择；若课程实验模拟的是关键状态复制，C 以少量时延换取更高可靠性，更符合共识系统的目标。数据也提醒我们，不能仅以平均时延判断方案优劣，失败重试、尾延迟和分区恢复成本同样重要。

## 四、可理解性带来的收益与代价

模块化设计提高了代码审查、测试覆盖和故障定位效率：选举测试可以围绕任期与投票，复制测试可以围绕索引与冲突回退，安全性测试则聚焦提交条件。但可理解不等于简单。定时器随机化、日志压缩、成员变更以及慢节点仍会引入复杂边界。工程实现必须坚持当前任期日志的提交规则，避免为了表面可用性放宽多数派约束，也应通过指标记录选举频率、复制落后量和提交时延。

## 结论

Raft 的核心价值是把共识安全性组织成可解释、可测试的机制。三节点分区表明只有多数派能够继续提交；实验数据则表明 B 适合极致时延，C 更适合可靠性优先的共识场景。工程决策应先保证选举与日志安全，再依据业务目标选择性能方案，并用故障注入验证分区与恢复行为。

## 参考资料

1. `materials/lecture_notes.md`，分布式系统课程笔记。
2. `materials/experiment_results.csv`，本次验收实验数据。
3. `course-report/references/checklist.md`，课程报告检查清单。
"""
                return self._call(
                    "overwrite_file",
                    {"path": "outputs/raft_course_report.md", "content": report},
                    "report-write",
                )
            return AgentResponse(
                final="课程报告草稿已保存到 outputs/raft_course_report.md。",
                finish_reason="stop",
            )
        if "must_not_exist.md" in prompt:
            if step == 0:
                return self._call(
                    "overwrite_file",
                    {
                        "path": "outputs/must_not_exist.md",
                        "content": "SHOULD-NOT-BE-WRITTEN",
                    },
                    "rejected-write",
                )
            return AgentResponse(final="写入请求已被拒绝，未创建文件。", finish_reason="stop")
        if "显式调用验证" in prompt:
            return AgentResponse(
                final="# 显式调用验证\n\n已通过 Gateway 加载 course-report 并执行任务。",
                finish_reason="stop",
            )
        return AgentResponse(final="LOCAL-SCRIPTED-UNHANDLED", finish_reason="stop")

    def chat(self, messages, **_kwargs):
        blob = json.dumps(messages, ensure_ascii=False)
        if "NEBULA-2049" in blob:
            return (
                "项目代号 NEBULA-2049；目标是降低选举抖动；不得牺牲日志安全性；"
                "三节点网络分区时只有多数派可以提交；表达上先给证据再给建议。"
            )
        return "验收任务"


def install_local_scripted_llm() -> LocalScriptedLLM:
    fake = LocalScriptedLLM()
    # Keep the existing RuntimeLLMClient object because Scheduler, compaction,
    # reflection and Gateway components already hold references to it.
    server._llm_client.chat_with_tools = fake.chat_with_tools
    server._llm_client.chat = fake.chat
    return fake


def record(task_id: str, name: str, passed: bool, evidence: Any, error: str = "") -> None:
    results.append(
        {
            "taskId": task_id,
            "name": name,
            "passed": bool(passed),
            "evidence": evidence,
            "error": error,
        }
    )
    print(f"[{task_id}] {'PASS' if passed else 'FAIL'} {name}", flush=True)


def create_session(client: TestClient, title: str) -> str:
    response = client.post("/sessions", json={"title": title})
    response.raise_for_status()
    return response.json()["sessionId"]


def chat(client: TestClient, session_id: str, message: str) -> dict[str, Any]:
    response = client.post(
        "/chat", json={"sessionId": session_id, "message": message}
    )
    response.raise_for_status()
    return response.json()


def command(client: TestClient, session_id: str, text: str) -> dict[str, Any]:
    response = client.post(
        "/command", json={"sessionId": session_id, "command": text}
    )
    response.raise_for_status()
    return response.json()


def chat_with_decisions(
    client: TestClient,
    session_id: str,
    message: str,
    *,
    decision: str = "approve",
    timeout: float = 180,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(chat, client, session_id, message)
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            response = client.get("/approvals", params={"sessionId": session_id})
            if response.status_code == 200:
                for approval in response.json().get("approvals", []):
                    aid = approval["approvalId"]
                    if any(item["approvalId"] == aid for item in seen):
                        continue
                    seen.append(approval)
                    endpoint = "approve" if decision == "approve" else "reject"
                    body = None if decision == "approve" else {"reason": "验收任务主动拒绝"}
                    decided = client.post(
                        f"/approvals/{aid}/{endpoint}", json=body
                    )
                    decided.raise_for_status()
            time.sleep(0.2)
        if not future.done():
            client.post("/stop", json={"sessionId": session_id})
            raise TimeoutError("chat did not finish before the approval deadline")
        return future.result(), seen


def assistant_reply(payload: dict[str, Any]) -> str:
    return str(payload.get("reply") or "")


def run() -> None:
    install_local_scripted_llm()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    generated = WORKSPACE / "outputs" / "raft_course_report.md"
    rejected = WORKSPACE / "outputs" / "must_not_exist.md"
    generated.unlink(missing_ok=True)
    rejected.unlink(missing_ok=True)

    with TestClient(app) as client:
        # T00: real LLM invocation and error handling surface.
        sid_base = create_session(client, "T00 基础调用")
        payload = chat(client, sid_base, "只回复 CLAW-BASE-OK，不要添加其他字符。")
        reply = assistant_reply(payload)
        record(
            "T00",
            "基础调用契约（本地确定性模型）",
            "CLAW-BASE-OK" in reply,
            {"reply": reply},
        )

        # T01: multi-turn history and deterministic reasoning over prior text.
        sid_multi = create_session(client, "T01 多轮对话")
        first = chat(
            client,
            sid_multi,
            "记住本 session 代号 ORBIT-731 和规则：后三位乘以 2。只回复“已记录”。",
        )
        second = chat(
            client,
            sid_multi,
            "不要复述规则，只给出本 session 代号后三位按规则计算后的数字。",
        )
        second_reply = assistant_reply(second)
        record(
            "T01",
            "多轮上下文记忆与计算",
            "1462" in second_reply,
            {"first": assistant_reply(first), "second": second_reply},
        )

        # T02: session isolation, persistence, rename, list and slash routing.
        sid_isolated = create_session(client, "T02 隔离会话")
        isolated = chat(
            client,
            sid_isolated,
            "本 session 是否有人告诉过你 ORBIT 开头的代号？若没有，只回复 UNKNOWN。",
        )
        isolated_reply = assistant_reply(isolated)
        slash = chat(client, sid_isolated, "/session list")
        renamed = client.patch(
            f"/sessions/{sid_isolated}", json={"title": "T02 已重命名"}
        )
        fresh_store = SessionStore(DATA_DIR / "sessions")
        persisted = fresh_store.exists(sid_multi) and fresh_store.exists(sid_isolated)
        session_list = client.get("/sessions").json().get("sessions", [])
        passed = (
            "ORBIT-731" not in isolated_reply
            and "UNKNOWN" in isolated_reply.upper()
            and slash.get("type") == "command"
            and renamed.status_code == 200
            and persisted
            and len(session_list) >= 3
        )
        record(
            "T02",
            "多 Session 隔离、持久化与内部命令",
            passed,
            {
                "isolatedReply": isolated_reply,
                "slashType": slash.get("type"),
                "persisted": persisted,
                "sessionCount": len(session_list),
            },
        )

        # T03: manual memory management plus cross-session stable context recall.
        memory_response = client.post(
            "/memories",
            json={
                "content": "用户写课程报告时偏好结论先行；验收标记是琥珀-417。",
                "category": "user_preference",
                "tags": ["course-report", "acceptance"],
                "importance": 5,
                "sourceSessionId": sid_multi,
            },
        )
        sid_memory = create_session(client, "T03 跨会话记忆")
        memory_chat = chat(
            client,
            sid_memory,
            "根据长期记忆，回答我写课程报告的结构偏好和验收标记。",
        )
        memory_reply = assistant_reply(memory_chat)
        memory_search = client.get("/memories/search", params={"q": "琥珀"}).json()
        record(
            "T03",
            "长期记忆 CRUD 与跨 Session 召回",
            memory_response.status_code == 200
            and "结论先行" in memory_reply
            and "417" in memory_reply
            and memory_search.get("count", 0) >= 1,
            {"reply": memory_reply, "searchCount": memory_search.get("count")},
        )

        # T04: seed a long current-session transcript, compact through Gateway,
        # and verify the summary survives a fresh SessionStore instance.
        sid_compact = create_session(client, "T04 压缩")
        compact_session = server._session_store.get(sid_compact)
        seeded = [
            ("user", "项目代号为 NEBULA-2049，目标是降低选举抖动。"),
            ("assistant", "已记录项目代号与目标。"),
            ("user", "硬约束：不得牺牲日志安全性。"),
            ("assistant", "已记录安全性约束。"),
            ("user", "偏好：所有结论先给证据再给建议。"),
            ("assistant", "已记录表达偏好。"),
            ("user", "风险：三节点集群在网络分区时只能由多数派提交。"),
            ("assistant", "已记录网络分区风险。"),
        ]
        for role, content in seeded:
            compact_session.append_message(role, content)
        padding = (
            "补充背景：系统需要在丢包、延迟和节点重启并存时保持任期单调、投票唯一、"
            "日志前缀一致与多数派提交，并记录可复现的观测证据。"
        )
        for index in range(12):
            compact_session.append_message(
                "user", f"压力测试背景 {index + 1}：{padding}{padding}"
            )
            compact_session.append_message(
                "assistant", f"已纳入第 {index + 1} 组背景，仍以 NEBULA-2049 的硬约束为准。"
            )
        server._session_store.save(compact_session)
        compact_result = command(client, sid_compact, "/compact")
        compact_loaded = SessionStore(DATA_DIR / "sessions").get(sid_compact)
        summary = compact_loaded.summary or ""
        compact_reply = chat(
            client,
            sid_compact,
            "项目代号是什么？只回复代号。",
        )
        record(
            "T04",
            "长对话压缩、摘要持久化与事实保留",
            "Summary updated: yes" in compact_result.get("result", "")
            and bool(summary)
            and "NEBULA-2049" in assistant_reply(compact_reply),
            {
                "command": compact_result.get("result", "")[:500],
                "summary": summary[:500],
                "recall": assistant_reply(compact_reply),
            },
        )

        # T05: bind a workspace and require evidence from three read-only tools.
        sid_tools = create_session(client, "T05 只读工具")
        ws_set = client.post(
            "/workspace", json={"sessionId": sid_tools, "path": str(WORKSPACE)}
        )
        tool_payload = chat(
            client,
            sid_tools,
            "必须真实调用工具：先列出 materials 目录，再读取 lecture_notes.md 和 "
            "experiment_results.csv。回答：推荐哪个 variant 及理由，并说明三节点网络分区时"
            "哪一侧能继续提交。不得凭常识跳过读取。",
        )
        tool_reply = assistant_reply(tool_payload)
        message_blob = json.dumps(tool_payload.get("messages", []), ensure_ascii=False)
        tool_signals = [name for name in ("list_dir", "read_file") if name in message_blob]
        record(
            "T05",
            "只读 Tool 与 observation 闭环",
            ws_set.status_code == 200
            and "B" in tool_reply
            and "82" in tool_reply
            and ("多数" in tool_reply or "majority" in tool_reply.lower())
            and len(tool_signals) == 2,
            {"reply": tool_reply, "toolSignals": tool_signals},
        )

        # T06: attachment metadata must stay session-scoped.
        upload = client.post(
            f"/sessions/{sid_tools}/attachments?persistMessage=false",
            files={"file": ("evidence.txt", b"ATTACHMENT-ONLY-T05", "text/plain")},
        )
        own = client.get(f"/sessions/{sid_tools}/attachments").json()
        other = client.get(f"/sessions/{sid_memory}/attachments").json()
        record(
            "T06",
            "Gateway 附件上传与 Session 隔离",
            upload.status_code == 200
            and len(own.get("attachments", [])) == 1
            and len(other.get("attachments", [])) == 0,
            {
                "ownCount": len(own.get("attachments", [])),
                "otherCount": len(other.get("attachments", [])),
            },
        )

        # T07: one-shot scheduled task must execute the shared agent loop and
        # append a result to the owning session.
        sid_cron = create_session(client, "T07 定时任务")
        run_at = (datetime.now(timezone.utc) + timedelta(seconds=7)).isoformat()
        cron_created = client.post(
            "/cron/jobs",
            json={
                "name": "acceptance-one-shot",
                "message": "定时验收：只回复 CRON-EXECUTED-2026。",
                "at": run_at,
                "sessionId": sid_cron,
            },
        )
        cron_job = cron_created.json().get("job", {})
        cron_id = cron_job.get("id", "")
        cron_final: dict[str, Any] = cron_job
        deadline = time.monotonic() + 60
        while cron_id and time.monotonic() < deadline:
            response = client.get(f"/cron/jobs/{cron_id}")
            if response.status_code == 404:  # delete-after-run one-shot
                break
            cron_final = response.json().get("job", {})
            history = cron_final.get("state", {}).get("runHistory", [])
            if history:
                break
            time.sleep(1)
        cron_messages = client.get(f"/sessions/{sid_cron}/messages").json().get(
            "messages", []
        )
        cron_blob = json.dumps(cron_messages, ensure_ascii=False)
        record(
            "T07",
            "一次性 Scheduler 任务与 Session 回写",
            cron_created.status_code == 200 and "CRON-EXECUTED-2026" in cron_blob,
            {
                "jobId": cron_id,
                "lastState": cron_final.get("state", {}),
                "messageTail": cron_messages[-4:],
            },
        )

        # T08/T09: explicit skill-oriented task, material reading, write
        # approval, and report generation.  The prompt requires the model to
        # expose tool/skill integration rather than merely drafting in chat.
        sid_skill = create_session(client, "T08-T09 Skill 与审批")
        client.post(
            "/workspace", json={"sessionId": sid_skill, "path": str(WORKSPACE)}
        ).raise_for_status()
        skill_payload, approvals = chat_with_decisions(
            client,
            sid_skill,
            "请完成课程报告草稿任务。必须先调用 skills_list，再调用 skill_view 加载 "
            "course-report（含其 checklist 参考文件），读取 materials/lecture_notes.md 和 "
            "materials/experiment_results.csv，然后生成一份 900—1200 字的中文 Markdown "
            "课程报告，主题为“Raft 的可理解性与工程权衡”。报告必须包含摘要、至少三个正文"
            "小节、三节点网络分区案例、实验 variant 对比、结论和参考资料。最后必须用 "
            "overwrite_file 保存到 outputs/raft_course_report.md，不要使用 shell。",
            decision="approve",
            timeout=240,
        )
        file_text = generated.read_text(encoding="utf-8") if generated.exists() else ""
        skill_messages = json.dumps(skill_payload.get("messages", []), ensure_ascii=False)
        required_sections = all(
            marker in file_text
            for marker in ("#", "摘要", "结论", "参考", "Raft", "82")
        ) and 900 <= len(file_text) <= 1800
        record(
            "T08",
            "Workspace 写入边界与 Approval 执行",
            bool(approvals) and generated.exists() and required_sections,
            {
                "approvals": approvals,
                "file": str(generated),
                "size": len(file_text),
                "reply": assistant_reply(skill_payload),
            },
        )
        record(
            "T09",
            "Skill 发现、加载、参考资源与产物",
            "skills_list" in skill_messages
            and "skill_view" in skill_messages
            and "course-report" in skill_messages
            and generated.exists(),
            {
                "skillSignals": [
                    name
                    for name in ("skills_list", "skill_view", "course-report")
                    if name in skill_messages
                ],
                "file": str(generated),
            },
        )

        # T08b: a rejected write must not create the target and the rejection
        # should become part of the session-visible execution outcome.
        reject_payload, rejected_approvals = chat_with_decisions(
            client,
            sid_skill,
            "使用 overwrite_file 创建 outputs/must_not_exist.md，内容为 SHOULD-NOT-BE-WRITTEN。",
            decision="reject",
            timeout=180,
        )
        reject_blob = json.dumps(reject_payload.get("messages", []), ensure_ascii=False)
        record(
            "T08R",
            "Approval 拒绝阻止副作用并写回历史",
            bool(rejected_approvals)
            and not rejected.exists()
            and ("拒绝" in reject_blob or "rejected" in reject_blob.lower()),
            {
                "approvals": rejected_approvals,
                "fileExists": rejected.exists(),
                "reply": assistant_reply(reject_payload),
            },
        )

        # Skill registry baseline and current-session usage visibility.
        skills = client.get("/skills").json().get("skills", [])
        usage = command(client, sid_skill, "/skill usage").get("result", "")
        record(
            "T09M",
            "至少三个 Skill 与使用记录入口",
            len(skills) >= 3 and "course-report" in {s.get("name") for s in skills},
            {"skills": [s.get("name") for s in skills], "usage": usage},
        )

        # The Gateway must consume the CLI sentinel and actually start a turn;
        # leaking it to a graphical client means explicit invocation is broken.
        explicit = command(
            client,
            sid_skill,
            "/skill course-report 只生成一段标题为显式调用验证的课程报告草稿，不写文件。",
        )
        explicit_result = str(explicit.get("result") or "")
        record(
            "T09E",
            "Gateway 显式 Skill 调用闭环",
            not explicit_result.startswith("__SKILL_INVOKE__")
            and "显式调用验证" in explicit_result,
            {"result": explicit_result},
        )

    summary = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataDir": str(DATA_DIR),
        "workspace": str(WORKSPACE),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "results": results,
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("passed", "failed")}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        record("RUNNER", "验收执行器", False, {}, f"{type(exc).__name__}: {exc}")
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps({"passed": 0, "failed": 1, "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
