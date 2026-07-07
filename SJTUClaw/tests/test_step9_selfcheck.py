"""Self-check for SJTUClaw Step 9 — Skill System."""
import json
import shutil
import tempfile
from pathlib import Path


def run():
    passed = 0; failed = 0
    def chk(label, cond, detail=""):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [PASS] {label}")
        else: failed += 1; print(f"  [FAIL] {label} -- {detail}")

    print("=" * 60)
    print("SJTUClaw Step 9 — Comprehensive Self-Check")
    print("=" * 60)

    # --- 1. Registry ---
    print("\n--- 1. Registry ---")
    from claw.skills.registry import SkillRegistry, SkillUsageRecord
    reg = SkillRegistry()
    skills = reg.list_skills()
    chk(">= 3 skills", len(skills) >= 3, str(len(skills)))
    for s in skills:
        chk(f"{s.name} has desc", bool(s.description))
        chk(f"{s.name} has instructions", len(s.instructions) > 50, f"{len(s.instructions)} chars")
    idx = reg.list_index()
    chk("index count matches", len(idx) == len(skills))
    chk("index has no instructions", all("instructions" not in str(e) for e in idx))
    full = reg.load_skill("course-report")
    chk("load returns full", len(full.instructions) > 100)
    chk("has references", len(full.references) > 0)
    formatted = reg.format_full_content("course-report")
    chk("formatted has instructions", "使用说明" in formatted)

    # --- 2. CLI commands ---
    print("\n--- 2. CLI commands ---")
    tmp = Path(tempfile.mkdtemp())
    from claw.session.store import SessionStore
    from claw.memory.store import MemoryStore
    from claw.context.builder import ContextBuilder
    from claw.cli.commands import handle_command, RuntimeState
    ss = SessionStore(tmp / "sess"); ms = MemoryStore(tmp / "mem.json")
    cb = ContextBuilder("sp","s",ms); cb.set_skill_registry(reg)
    sess = ss.create_session(); sid = sess.session_id

    class FC:
        def chat(self, m): return "ok"
        def chat_with_tools(self, m, t):
            from claw.llm.protocol import AgentResponse
            return AgentResponse(final="ok")

    state = RuntimeState(session_store=ss, memory_store=ms, llm_client=FC(),
                          current_session_id=sid, skill_registry=reg)
    result = handle_command("/skill list", state)
    chk("/skill list has names", all(s.name in result for s in skills))
    result = handle_command("/skill show course-report", state)
    chk("/skill show has instructions", "使用说明" in result)
    result = handle_command("/skill show unknown-x", state)
    chk("/skill show unknown errors", "未找到" in result)
    result = handle_command("/skill usage", state)
    chk("/skill usage empty OK", "暂无" in result)

    # --- 3. Context builder ---
    print("\n--- 3. Context builder ---")
    msgs = cb.build_messages(sess)
    all_txt = " ".join(m["content"] for m in msgs)
    chk("has skill index", "可用 Skills" in all_txt)
    chk("has course-report name", "course-report" in all_txt)
    chk("has material-summary", "material-summary" in all_txt)
    chk("has use_skill hint", "use_skill" in all_txt)
    chk("NO full instructions leaked", full.instructions[:100] not in all_txt)

    # --- 4. Usage recording ---
    print("\n--- 4. Usage recording ---")
    sess.skill_usage.append(SkillUsageRecord(
        skill_name="course-report", session_id=sid, user_task="write report",
        source="explicit", used_at="2026-07-07T10:00:00").to_dict())
    sess.skill_usage.append(SkillUsageRecord(
        skill_name="material-summary", session_id=sid, user_task="summarize",
        source="auto", auto_reason="multi-file summarization fit",
        used_at="2026-07-07T10:30:00").to_dict())
    chk("2 usage records", len(sess.skill_usage) == 2)
    result = handle_command("/skill usage", state)
    chk("shows explicit", "显式调用" in result or "explicit" in result)
    chk("shows auto", "模型自主" in result or "auto" in result)
    chk("shows reason", "summarization" in result)
    ss.save(sess)
    loaded = ss.get(sid)
    chk("persists usage", len(loaded.skill_usage) == 2)

    # --- 5. use_skill tool ---
    print("\n--- 5. use_skill tool ---")
    from claw.tools.base import ToolRegistry
    from claw.workspace.manager import WorkspaceManager
    from claw.config import SESSIONS_DIR
    from claw.tools import register_all_tools
    wm = WorkspaceManager(); wm.set(sid, str(tmp))
    treg = ToolRegistry()
    register_all_tools(treg, workspace_manager=wm, session_id_provider=lambda: sid, sessions_dir=SESSIONS_DIR, include_skill_tool=True)
    chk("use_skill registered", "use_skill" in treg.list_tool_names())
    t = treg.get_tool("use_skill")
    chk("safety_level=skill_select", t.safety_level == "skill_select")

    # --- 6. Injection format ---
    print("\n--- 6. Injection format ---")
    injected = cb.build_skill_injection_message("course-report", "write a report")
    chk("has skill name", "course-report" in injected)
    chk("has instructions block", "使用说明" in injected)
    chk("has user task", "write a report" in injected)

    # --- 7. Explicit sentinel ---
    print("\n--- 7. Explicit sentinel ---")
    result = handle_command("/skill course-report write a course report about AI, save as r.md", state)
    chk("returns SKILL_INVOKE", result.startswith("__SKILL_INVOKE__|"))
    if result.startswith("__SKILL_INVOKE__|"):
        parts = result.split("|")
        chk("has skill_name", parts[1] == "course-report")
        chk("has task", "AI" in parts[2])

    wm.unset(sid); shutil.rmtree(str(tmp), ignore_errors=True)
    print(f"\n{'='*60}\nRESULTS: {passed}/{passed+failed} passed\n{'='*60}")
    return failed == 0

if __name__ == "__main__":
    import sys; sys.exit(0 if run() else 1)
