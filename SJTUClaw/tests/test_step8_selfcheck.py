"""Self-check for SJTUClaw Step 8 — Workspace, Tools, Approval, Shell."""
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path


def run():
    passed = 0; failed = 0
    def chk(label, cond, detail=""):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [PASS] {label}")
        else: failed += 1; print(f"  [FAIL] {label} -- {detail}")

    tmp = Path(tempfile.mkdtemp())
    sid = f"chk_{uuid.uuid4().hex[:8]}"
    sid_b = f"chk_{uuid.uuid4().hex[:8]}"

    from claw.tools.base import ToolRegistry
    from claw.workspace.manager import WorkspaceManager
    from claw.config import SESSIONS_DIR
    from claw.tools import register_all_tools
    from claw.approval.manager import ApprovalManager, ApprovalStatus
    from claw.tools.shell import _shell_sessions

    wm = WorkspaceManager(); am = ApprovalManager()
    reg = ToolRegistry()
    register_all_tools(reg, workspace_manager=wm, session_id_provider=lambda: sid, sessions_dir=SESSIONS_DIR)
    reg_b = ToolRegistry()
    register_all_tools(reg_b, workspace_manager=wm, session_id_provider=lambda: sid_b, sessions_dir=SESSIONS_DIR)

    print("=" * 60)
    print("SJTUClaw Step 8 — Comprehensive Self-Check")
    print("=" * 60)

    # --- 1. No workspace → rejects ---
    print("\n--- 1. No workspace ---")
    for tool, args in [("create_file", {"path": "t.txt"}), ("overwrite_file", {"path": "x", "content": "h"}), ("new_shell", {}), ("create_download", {"path": "x"})]:
        r = reg.execute_by_name(tool, args)
        chk(f"{tool} rejected", not r.ok, str(r.error)[:60] if r.error else "")
    chk("error mentions workspace", any("workspace" in str(reg.execute_by_name(t, a).error or "").lower() for t, a in [("create_file", {"path":"t.txt"}), ("new_shell", {})]))

    # --- 2. Set workspace → approve → file ---
    print("\n--- 2. Set workspace + approve ---")
    wm.set(sid, str(tmp))
    chk("workspace set OK", wm.get(sid) is not None)
    req = am.create(sid, "create_file", {"path": "hw.py"})
    am.approve(req.approval_id)
    chk("approval approved", am.get(req.approval_id).status == ApprovalStatus.APPROVED.value)
    r = reg.execute_by_name("create_file", {"path": "hw.py"})
    chk("create_file OK", r.ok)
    target = tmp / "hw.py"
    chk("file exists on disk", target.exists())
    chk("result has tool name", r.content and "create_file" in r.content, str(r.content)[:80] if r.content else "")

    # --- 3. Reject → file unchanged ---
    print("\n--- 3. Approval rejection ---")
    target.write_text("original", encoding="utf-8")
    req2 = am.create(sid, "overwrite_file", {"path": "hw.py", "content": "BAD"})
    am.reject(req2.approval_id, "don't modify")
    chk("status rejected", am.get(req2.approval_id).status == ApprovalStatus.REJECTED.value)
    chk("file unchanged", target.read_text(encoding="utf-8") == "original")

    # --- 4. Path escape ---
    print("\n--- 4. Path escape ---")
    for args in [{"path": "../../evil"}, {"path": "C:/Windows/hack"}]:
        r = reg.execute_by_name("create_file", args)
        chk(f"escape {args['path'][:30]} rejected", not r.ok)
    chk("overwrite escape rejected", not reg.execute_by_name("overwrite_file", {"path": "../e", "content":"x"}).ok)
    chk("download escape rejected", not reg.execute_by_name("create_download", {"path": "../s"}).ok)

    # --- 5. Shell ---
    print("\n--- 5. Shell ---")
    r = reg.execute_by_name("new_shell", {})
    chk("new_shell OK", r.ok, str(r.error)[:80] if r.error else "")
    r = reg.execute_by_name("run_command", {"command": "echo first"})
    chk("echo first OK", r.ok)
    chk("stdout correct", r.content and "first" in r.content)

    sub = tmp / "sub"; sub.mkdir(exist_ok=True)
    r = reg.execute_by_name("run_command", {"command": f"cd /d {sub}"})
    chk("cd to sub OK", r.ok)
    r = reg.execute_by_name("run_command", {"command": "echo %CD%"})
    c2 = json.loads(r.content) if r.ok and r.content else {}
    chk("cwd updated", Path(c2.get("stdout", "").strip()).resolve() == sub.resolve(),
        f"{c2.get('stdout','').strip()} vs {sub}")

    r = reg.execute_by_name("run_command", {"command": "cd /d C:\\Windows"})
    chk("cd escape rejected pre-exec", not r.ok and ("拒绝" in str(r.error)), str(r.error)[:80] if r.error else "OK=True")
    chk("shell alive after reject", sid in _shell_sessions)

    # --- 6. Download ---
    print("\n--- 6. Download ---")
    (tmp / "dl.md").write_text("# DL test", encoding="utf-8")
    r = reg.execute_by_name("create_download", {"path": "dl.md"})
    chk("create_download OK", r.ok)
    dl_id = json.loads(r.content).get("downloadId", "") if r.ok and r.content else ""
    from claw.tools.download import get_download
    dp = get_download(dl_id)
    chk("downloadId resolves", dp is not None)
    chk("content correct", dp is not None and dp.read_text(encoding="utf-8") == "# DL test")

    # --- 7. Attachment isolation ---
    print("\n--- 7. Attachment isolation ---")
    att_dir = SESSIONS_DIR / sid / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / "att_x.txt").write_bytes(b"secret")
    (att_dir / ".meta.json").write_text(json.dumps([{"id":"att_x","originalName":"s.txt","storedName":"att_x.txt","size":6,"mimeType":"t","uploadedAt":""}]), encoding="utf-8")
    wm.set(sid_b, str(tmp))
    r = reg_b.execute_by_name("copy_attachment_to_workspace", {"attachment_id": "att_x", "dest_path": "steal.txt"})
    chk("cross-session blocked", not r.ok)
    chk("isolation message clear", r.error and ("其他 session" in r.error or "不在当前" in r.error), str(r.error)[:100] if r.error else "")

    # --- 8. Regression ---
    print("\n--- 8. Regression ---")
    for tool, args in [("current_time", {}), ("list_dir", {"path": "."}), ("read_file", {"path": "claw/config.py"})]:
        r = reg.execute_by_name(tool, args)
        chk(f"{tool} OK", r.ok)
    from claw.memory.store import MemoryStore
    from claw.session.store import SessionStore
    from claw.context.builder import ContextBuilder
    ms2 = MemoryStore(tmp / "mem.json"); ms2.add("regression test")
    ss2 = SessionStore(tmp / "sess"); s2 = ss2.create_session()
    s2.append_message("user","hi"); s2.append_message("assistant","ok")
    chk("session msgs correct", len(s2.messages) == 2)
    cb2 = ContextBuilder("sp","s",ms2); msgs = cb2.build_messages(s2)
    chk("context builder OK", len(msgs) >= 4)
    from claw.context.compaction import needs_compaction
    chk("compaction check OK", not needs_compaction(s2))

    # Cleanup
    wm.unset(sid); wm.unset(sid_b)
    for k in list(_shell_sessions): _shell_sessions[k].terminate()
    _shell_sessions.clear()
    shutil.rmtree(str(SESSIONS_DIR / sid), ignore_errors=True)
    shutil.rmtree(str(SESSIONS_DIR / sid_b), ignore_errors=True)
    shutil.rmtree(str(tmp), ignore_errors=True)

    print(f"\n{'='*60}\nRESULTS: {passed}/{passed+failed} passed\n{'='*60}")
    return failed == 0

if __name__ == "__main__":
    import sys; sys.exit(0 if run() else 1)
