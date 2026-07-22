"""Second-round advanced acceptance tests for SJTUClaw added features."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "claw_evaluation_workspace_round2"
RESULT_PATH = ROOT / "evaluation" / "claw_acceptance_round2_results.json"

raw_data_dir = os.environ.get("SJTUCLAW_DATA_DIR", "").strip()
if not raw_data_dir:
    raise SystemExit("Refusing to run without isolated SJTUCLAW_DATA_DIR")
DATA_DIR = Path(raw_data_dir).resolve()
if DATA_DIR == (ROOT / "data").resolve():
    raise SystemExit("Refusing to use normal project data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

from run_claw_acceptance import LocalScriptedLLM  # noqa: E402
from claw.gateway import server  # noqa: E402
from claw.gateway.server import app  # noqa: E402
from claw.llm.protocol import AgentResponse, ToolCallRequest  # noqa: E402


class AdvancedLocalLLM(LocalScriptedLLM):
    def __init__(self) -> None:
        super().__init__()
        self.advanced_steps: dict[str, int] = {}

    def _advanced_call(self, marker: str, name: str, args: dict[str, Any]):
        return AgentResponse(
            tool_calls=[
                ToolCallRequest(
                    name=name,
                    args=args,
                    call_id=f"round2-{marker}-{self.advanced_steps.get(marker, 0)}",
                )
            ],
            finish_reason="tool_calls",
        )

    def chat_with_tools(self, messages, tool_defs, **kwargs):
        blob = json.dumps(messages, ensure_ascii=False)
        markers = (
            "AUTO-WRITE",
            "AUTO-ESCAPE",
            "UNLIMITED-WRITE",
            "ROLLBACK-V1",
            "ROLLBACK-V2",
            "WEB-SSRF",
            "LONG-RUN",
            "HEARTBEAT-ROUND2",
        )
        present = [value for value in markers if value in blob]
        marker = max(present, key=blob.rfind) if present else ""
        if not marker:
            return super().chat_with_tools(messages, tool_defs, **kwargs)

        step = self.advanced_steps.get(marker, 0)
        self.advanced_steps[marker] = step + 1
        if marker == "AUTO-WRITE":
            if step == 0:
                return self._advanced_call(
                    marker,
                    "overwrite_file",
                    {"path": "auto/created.txt", "content": "AUTO-WRITE-SUCCESS"},
                )
            return AgentResponse(final="AUTO workspace 写入完成。", finish_reason="stop")
        if marker == "AUTO-ESCAPE":
            if step == 0:
                return self._advanced_call(
                    marker,
                    "overwrite_file",
                    {
                        "path": str((WORKSPACE / "outside-zone" / "escape.txt").resolve()),
                        "content": "MUST-NOT-ESCAPE",
                    },
                )
            return AgentResponse(final="越界写入已被 workspace 拒绝。", finish_reason="stop")
        if marker == "UNLIMITED-WRITE":
            if step == 0:
                return self._advanced_call(
                    marker,
                    "overwrite_file",
                    {
                        "path": str((WORKSPACE / "outside-zone" / "allowed.txt").resolve()),
                        "content": "UNLIMITED-APPROVED",
                    },
                )
            return AgentResponse(final="UNLIMITED 写入经审批完成。", finish_reason="stop")
        if marker == "ROLLBACK-V1":
            if step == 0:
                return self._advanced_call(
                    marker,
                    "overwrite_file",
                    {"path": "state.txt", "content": "VERSION-1"},
                )
            return AgentResponse(final="已写入 VERSION-1。", finish_reason="stop")
        if marker == "ROLLBACK-V2":
            if step == 0:
                return AgentResponse(
                    tool_calls=[
                        ToolCallRequest(
                            name="overwrite_file",
                            args={"path": "state.txt", "content": "VERSION-2"},
                            call_id="round2-state-v2",
                        ),
                        ToolCallRequest(
                            name="overwrite_file",
                            args={"path": "branch-only.txt", "content": "BRANCH"},
                            call_id="round2-branch",
                        ),
                    ],
                    finish_reason="tool_calls",
                )
            return AgentResponse(final="已写入 VERSION-2 与分支文件。", finish_reason="stop")
        if marker == "WEB-SSRF":
            if step == 0:
                return self._advanced_call(
                    marker,
                    "web_fetch",
                    {"url": "http://127.0.0.1:8765/private", "max_chars": 2000},
                )
            return AgentResponse(final="本地/私网目标已被 SSRF 防护阻止。", finish_reason="stop")
        if marker == "LONG-RUN":
            time.sleep(1.2)
            return AgentResponse(final="不应作为正常完成结果", finish_reason="stop")
        if marker == "HEARTBEAT-ROUND2":
            return AgentResponse(
                final="告警：ROUND2 发布清单仍有未完成项。", finish_reason="stop"
            )
        return super().chat_with_tools(messages, tool_defs, **kwargs)

    def chat(self, messages, **kwargs):
        blob = json.dumps(messages, ensure_ascii=False)
        if "记忆整理助手" in blob:
            return json.dumps(
                [
                    {
                        "category": "decision",
                        "content": "ROUND2 项目决定采用蓝绿色发布策略。",
                        "tags": ["round2", "deployment"],
                        "importance": 5,
                    }
                ],
                ensure_ascii=False,
            )
        return super().chat(messages, **kwargs)


results: list[dict[str, Any]] = []


def record(task_id: str, name: str, passed: bool, evidence: Any):
    results.append(
        {"taskId": task_id, "name": name, "passed": bool(passed), "evidence": evidence}
    )
    print(f"[{task_id}] {'PASS' if passed else 'FAIL'} {name}", flush=True)


def create_session(client: TestClient, title: str) -> str:
    response = client.post("/sessions", json={"title": title})
    response.raise_for_status()
    return response.json()["sessionId"]


def command(client: TestClient, sid: str, value: str) -> dict[str, Any]:
    response = client.post("/command", json={"sessionId": sid, "command": value})
    response.raise_for_status()
    return response.json()


def chat(client: TestClient, sid: str, value: str) -> dict[str, Any]:
    response = client.post("/chat", json={"sessionId": sid, "message": value})
    response.raise_for_status()
    return response.json()


def monitored_chat(
    client: TestClient,
    sid: str,
    message: str,
    *,
    decision: str | None,
    timeout: float = 20,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    approvals: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(chat, client, sid, message)
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            pending = client.get("/approvals", params={"sessionId": sid}).json().get(
                "approvals", []
            )
            for item in pending:
                if any(a["approvalId"] == item["approvalId"] for a in approvals):
                    continue
                approvals.append(item)
                if decision is None:
                    client.post(
                        f"/approvals/{item['approvalId']}/reject",
                        json={"reason": "unexpected approval"},
                    )
                else:
                    client.post(f"/approvals/{item['approvalId']}/{decision}", json={})
            time.sleep(0.05)
        if not future.done():
            client.post("/stop", json={"sessionId": sid})
            raise TimeoutError(message)
        return future.result(), approvals


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def run() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    fake = AdvancedLocalLLM()
    server._llm_client.chat_with_tools = fake.chat_with_tools
    server._llm_client.chat = fake.chat

    with TestClient(app) as client:
        # N01: AUTO must remove approval friction but retain the workspace wall.
        auto_root = WORKSPACE / "auto-root"
        auto_root.mkdir(parents=True, exist_ok=True)
        sid_auto = create_session(client, "Round2 AUTO/UNLIMITED")
        client.post("/workspace", json={"sessionId": sid_auto, "path": str(auto_root)}).raise_for_status()
        auto_on = command(client, sid_auto, "/auto on")
        auto_reply, auto_approvals = monitored_chat(
            client, sid_auto, "执行 AUTO-WRITE", decision=None
        )
        auto_file = auto_root / "auto" / "created.txt"
        record(
            "N01",
            "AUTO 免审批写入且保持 Workspace 边界",
            auto_on.get("autoMode") is True
            and not auto_approvals
            and auto_file.read_text(encoding="utf-8") == "AUTO-WRITE-SUCCESS",
            {"approvals": auto_approvals, "reply": auto_reply.get("reply")},
        )

        escape_reply, escape_approvals = monitored_chat(
            client, sid_auto, "执行 AUTO-ESCAPE", decision=None
        )
        escaped = WORKSPACE / "outside-zone" / "escape.txt"
        record(
            "N02",
            "AUTO 无法绕过 Workspace 路径边界",
            not escape_approvals and not escaped.exists(),
            {"approvals": escape_approvals, "reply": escape_reply.get("reply")},
        )

        # N03: UNLIMITED overrides the path wall, but even AUTO cannot skip approval.
        unlimited_on = command(client, sid_auto, "/unlimited on")
        unlimited_reply, unlimited_approvals = monitored_chat(
            client, sid_auto, "执行 UNLIMITED-WRITE", decision="approve"
        )
        outside_file = WORKSPACE / "outside-zone" / "allowed.txt"
        record(
            "N03",
            "UNLIMITED + AUTO 仍强制审批外部写入",
            unlimited_on.get("unlimitedMode") is True
            and bool(unlimited_approvals)
            and outside_file.read_text(encoding="utf-8") == "UNLIMITED-APPROVED",
            {
                "approvalTools": [a.get("toolName") for a in unlimited_approvals],
                "reply": unlimited_reply.get("reply"),
            },
        )

        # N04: rollback must atomically restore files and conversation; undo reverses it.
        rollback_root = WORKSPACE / "rollback-root"
        rollback_root.mkdir(parents=True, exist_ok=True)
        sid_rollback = create_session(client, "Round2 原子回退")
        client.post(
            "/workspace", json={"sessionId": sid_rollback, "path": str(rollback_root)}
        ).raise_for_status()
        command(client, sid_rollback, "/auto on")
        chat(client, sid_rollback, "执行 ROLLBACK-V1")
        chat(client, sid_rollback, "执行 ROLLBACK-V2")
        state_file = rollback_root / "state.txt"
        branch_file = rollback_root / "branch-only.txt"
        before_messages = client.get(f"/sessions/{sid_rollback}/messages").json()["messages"]
        preview = client.post(f"/sessions/{sid_rollback}/rollback/preview", json={}).json()
        applied = client.post(f"/sessions/{sid_rollback}/rollback", json={}).json()
        restored_ok = state_file.read_text(encoding="utf-8") == "VERSION-1" and not branch_file.exists()
        undo = client.post(f"/sessions/{sid_rollback}/rollback/undo").json()
        undo_ok = state_file.read_text(encoding="utf-8") == "VERSION-2" and branch_file.exists()
        record(
            "N04",
            "Workspace 文件与对话原子回退及单步撤销",
            restored_ok
            and undo_ok
            and len(applied.get("messages", [])) < len(before_messages)
            and bool(preview.get("preview")),
            {
                "preview": preview.get("preview"),
                "messagesBefore": len(before_messages),
                "messagesAfter": len(applied.get("messages", [])),
                "undo": undo.get("result"),
            },
        )

        # N05: daily Reflection extracts a durable decision from conversations.
        sid_reflect = create_session(client, "Round2 Reflection")
        session = server._session_store.get(sid_reflect)
        session.append_message("user", "ROUND2 项目决定采用蓝绿色发布策略，请长期记住。")
        session.append_message("assistant", "已记录这项发布决策。")
        server._session_store.save(session)
        reflection = client.post("/reflect/run").json()
        recalled = client.get("/memories/search", params={"q": "蓝绿色"}).json()
        record(
            "N05",
            "Reflection 抽取结构化长期记忆并记录运行历史",
            reflection.get("ok") is True
            and reflection.get("factsExtracted", 0) >= 1
            and recalled.get("count", 0) >= 1,
            {"reflection": reflection, "recallCount": recalled.get("count")},
        )

        # N06: Heartbeat reads active tasks and dispatches the shared agent loop.
        heartbeat_file = WORKSPACE / "HEARTBEAT.md"
        heartbeat_file.write_text(
            "# Monitor\n\n## Active Tasks\n\n- HEARTBEAT-ROUND2：检查发布清单。\n",
            encoding="utf-8",
        )
        if not server._session_store.exists("heartbeat"):
            server._session_store.create_session(session_id="heartbeat", title="Heartbeat")
        from claw.scheduler.callbacks import HeartbeatCallback, make_heartbeat_system_job

        callback = HeartbeatCallback(
            WORKSPACE,
            server._session_store,
            server._context_builder,
            server._tool_registry,
            server._llm_client,
        )
        job = make_heartbeat_system_job(SimpleNamespace(interval_s=1800))
        heartbeat_reply = asyncio.run(callback(job))
        record(
            "N06",
            "Heartbeat 活跃任务检测与 Agent 回写",
            heartbeat_reply is not None and "未完成" in heartbeat_reply,
            {"reply": heartbeat_reply},
        )

        # N07: recurring cron executes twice, then disabling prevents future runs.
        sid_cron = create_session(client, "Round2 周期任务")
        created = client.post(
            "/cron/jobs",
            json={
                "name": "round2-recurring",
                "message": "定时验收 ROUND2-CRON",
                "everySeconds": 2,
                "sessionId": sid_cron,
            },
        ).json()
        cron_id = created["job"]["id"]
        history: list[dict[str, Any]] = []
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job_data = client.get(f"/cron/jobs/{cron_id}").json()["job"]
            history = job_data["state"]["runHistory"]
            if len(history) >= 2:
                break
            time.sleep(0.5)
        client.post(f"/cron/jobs/{cron_id}/disable").raise_for_status()
        count_disabled = len(history)
        time.sleep(2.5)
        after_disable = client.get(f"/cron/jobs/{cron_id}").json()["job"]
        stable = len(after_disable["state"]["runHistory"]) == count_disabled
        client.delete(f"/cron/jobs/{cron_id}").raise_for_status()
        record(
            "N07",
            "周期 Scheduler 多次执行、禁用与删除",
            len(history) >= 2 and stable,
            {"runs": len(history), "stableAfterDisable": stable},
        )

        # N08: sensitive runtime settings are encrypted and only masked by API.
        qq_secret = "ROUND2-QQ-SECRET-938475"
        qq_update = client.put(
            "/settings/channel/qq",
            json={
                "enabled": False,
                "appId": "round2-app",
                "clientSecret": qq_secret,
                "allowFrom": "trusted-user",
                "msgFormat": "markdown",
                "ackMessage": "处理中",
            },
        ).json()
        from claw.runtime_settings import SETTINGS_PATH

        raw_settings = SETTINGS_PATH.read_text(encoding="utf-8")
        masked = qq_update["settings"]["qq"]["clientSecretMasked"]
        invalid_avatar = client.put(
            "/settings/ui/avatar",
            json={"avatarId": "custom", "customImage": "not-a-data-url"},
        )
        record(
            "N08",
            "运行时敏感配置加密、脱敏与输入校验",
            qq_secret not in raw_settings
            and "encrypted" in raw_settings
            and masked != qq_secret
            and invalid_avatar.status_code == 400,
            {"masked": masked, "invalidAvatarStatus": invalid_avatar.status_code},
        )

        # N09: pet catalog/settings work; hostile archives are rejected safely.
        pets = client.get("/pet/pets").json().get("pets", [])
        pet_id = pets[0]["id"] if pets else ""
        pet_update = client.put("/pet/settings", json={"selectedPetId": pet_id})
        hostile_pet = zip_bytes(
            {
                "../escape.txt": b"escape",
                "pet.json": b'{"id":"round2-pet","displayName":"R2","spriteVersionNumber":2,"spritesheetPath":"spritesheet.png"}',
                "spritesheet.png": b"not-an-image",
            }
        )
        rejected_pet = client.post(
            "/pet/pets",
            files={"package": ("hostile.zip", hostile_pet, "application/zip")},
        )
        record(
            "N09",
            "桌宠目录、选择持久化与恶意 ZIP 防护",
            bool(pets) and pet_update.status_code == 200 and rejected_pet.status_code == 400,
            {
                "petCount": len(pets),
                "selected": pet_id,
                "hostileStatus": rejected_pet.status_code,
            },
        )

        # N10: a model-requested localhost fetch is blocked before networking.
        sid_web = create_session(client, "Round2 SSRF")
        web_reply = chat(client, sid_web, "执行 WEB-SSRF")
        web_blob = json.dumps(web_reply.get("messages", []), ensure_ascii=False)
        record(
            "N10",
            "Web Tool SSRF 私网阻断与 observation 回写",
            "web_fetch" in web_blob
            and ("私网" in web_blob or "localhost" in web_blob or "127.0.0.1" in web_blob),
            {"reply": web_reply.get("reply")},
        )

        # N11: concurrent turn is rejected; /stop terminates the active session.
        sid_stop = create_session(client, "Round2 停止与并发")
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(chat, client, sid_stop, "执行 LONG-RUN")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if server._session_turn_active(sid_stop):
                    break
                time.sleep(0.02)
            concurrent = client.post(
                "/chat", json={"sessionId": sid_stop, "message": "并发请求"}
            )
            stopped = client.post("/stop", json={"sessionId": sid_stop}).json()
            final_long = running.result(timeout=5)
        stop_blob = json.dumps(final_long.get("messages", []), ensure_ascii=False)
        record(
            "N11",
            "同 Session 并发互斥与用户停止",
            concurrent.status_code == 409
            and stopped.get("cancelled") == 1
            and ("终止" in stop_blob or "cancel" in stop_blob.lower()),
            {
                "concurrentStatus": concurrent.status_code,
                "stop": stopped,
                "reply": final_long.get("reply"),
            },
        )

        # N12: hot install/list/delete a skill in an isolated skill root.
        import claw.skills.management as skill_management
        import claw.skills.registry as skill_registry_module
        from claw.skills.registry import SkillRegistry

        isolated_skills = DATA_DIR / "uploaded-skills"
        skill_management.SKILLS_DIR = isolated_skills
        skill_registry_module.SKILLS_DIR = isolated_skills
        isolated_registry = SkillRegistry()
        server._skill_registry = isolated_registry
        server._context_builder.set_skill_registry(isolated_registry)
        package = zip_bytes(
            {
                "round2-audit/SKILL.md": (
                    "---\nname: round2-audit\ndescription: 对发布清单进行严格审计。\n---\n\n"
                    "# Round2 Audit\n\n逐项检查输入、证据、风险和回退方案。\n"
                ).encode("utf-8"),
                "round2-audit/references/checklist.md": "# Checklist\n- evidence\n".encode("utf-8"),
            }
        )
        installed = client.post(
            "/skills/upload",
            files={"file": ("round2-audit.zip", package, "application/zip")},
        )
        listed = client.get("/skills").json().get("skills", [])
        detail = client.get("/skills/round2-audit")
        deleted = client.delete("/skills/round2-audit")
        listed_after = client.get("/skills").json().get("skills", [])
        record(
            "N12",
            "Skill 安装、热加载、详情与安全删除生命周期",
            installed.status_code == 200
            and any(s.get("name") == "round2-audit" for s in listed)
            and detail.status_code == 200
            and deleted.status_code == 200
            and not any(s.get("name") == "round2-audit" for s in listed_after),
            {
                "installStatus": installed.status_code,
                "detailStatus": detail.status_code,
                "deleteStatus": deleted.status_code,
            },
        )

    summary = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataDir": str(DATA_DIR),
        "workspace": str(WORKSPACE),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }
    RESULT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "failed": summary["failed"]}))


if __name__ == "__main__":
    run()
