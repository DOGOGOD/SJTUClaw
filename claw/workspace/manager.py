"""Workspace manager: per-session workspace binding with path sandboxing
and persistent storage.

Enforces that all file operations within a session are confined to a
designated workspace directory.  Path-traversal (``../``) and absolute
paths are rejected before any file I/O is attempted.

Bindings are persisted to ``data/workspace/bindings.json`` so sessions
automatically re-bind to their workspace after a gateway restart.  Users
can still change or unset the workspace with ``/workspace`` commands.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from claw.config import DATA_DIR


# ---------------------------------------------------------------------------
# Storage path
# ---------------------------------------------------------------------------

_BINDINGS_PATH = DATA_DIR / "workspace" / "bindings.json"
# Multiple WorkspaceManager instances exist in CLI/Gateway tests and may save
# the same process-wide bindings file concurrently.  An instance lock alone
# cannot serialize those writers.
_PERSIST_LOCK = threading.RLock()


def _atomic_replace_with_retry(source: Path, destination: Path) -> None:
    """Replace *destination*, tolerating short-lived Windows file locks.

    Antivirus/indexing software can briefly open the just-written JSON file
    without delete sharing, causing ``os.replace`` to raise WinError 5 even
    though our application writers are serialized.  Retrying only this known
    transient error keeps the write atomic and still fails promptly for real
    permission problems.
    """
    attempts = 7 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.01 * (2**attempt), 0.25))


class WorkspaceError(RuntimeError):
    """Raised when a workspace operation is invalid (unset, out-of-bounds)."""


class WorkspaceManager:
    """Per-session workspace binding — persistent across restarts.

    Thread-safe: concurrent HTTP + QQ + cron threads may bind, resolve,
    and unbind workspaces simultaneously.

    Usage::

        wm = WorkspaceManager()
        wm.set("s1", "/tmp/ws1")         # bind + persist
        wm.resolve("s1", "foo/bar.txt")  # safe resolve inside workspace
        wm.require("s1")                 # workspace root (raise if unset)
        wm.get("s1")                     # workspace root (or None)
        wm.unset("s1")                   # unbind + remove from disk
        wm.set_unlimited("s1", True)     # bypass workspace checks for s1
        wm.is_unlimited("s1")            # check if unlimited
    """

    def __init__(self) -> None:
        self._workspaces: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._unlimited_sessions: set[str] = set()
        self._load()

    # -- unlimited mode -----------------------------------------------------------

    def set_unlimited(self, session_id: str, unlimited: bool) -> None:
        """Enable or disable unrestricted mode for *session_id*.

        When enabled, workspace boundary enforcement is bypassed: the agent
        can read, write, and execute commands on any path in the filesystem,
        regardless of the workspace binding.
        """
        with self._lock:
            if unlimited:
                self._unlimited_sessions.add(session_id)
            else:
                self._unlimited_sessions.discard(session_id)

    def is_unlimited(self, session_id: str) -> bool:
        """Return True if *session_id* has unlimited mode enabled."""
        with self._lock:
            return session_id in self._unlimited_sessions

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        """Load saved bindings from disk on startup.

        Always restores every binding from disk — even when the bound
        directory does not currently exist.  This ensures a gateway
        restart never silently drops session → workspace mappings.
        """
        with _PERSIST_LOCK:
            if not _BINDINGS_PATH.exists():
                return
            try:
                raw = _BINDINGS_PATH.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                return

        if not isinstance(data, dict):
            return

        loaded = 0
        missing = 0
        for sid, ws_path in data.items():
            if not isinstance(sid, str) or not isinstance(ws_path, str):
                continue
            p = Path(ws_path)
            self._workspaces[sid] = p
            loaded += 1
            try:
                available_directory = p.exists() and p.is_dir()
            except OSError:
                # A persisted path can become inaccessible between runs
                # (revoked ACL, disconnected drive, protected test temp dir,
                # etc.).  Keep the binding for possible later recovery, but
                # never let one unusable workspace prevent gateway startup.
                available_directory = False
            if not available_directory:
                missing += 1

        if loaded:
            detail = f"已恢复 {loaded} 个 workspace 绑定"
            if missing:
                detail += f"，其中 {missing} 个目录当前不存在"
            print(f"[workspace] {detail}")

    def _save(self) -> None:
        """Persist current bindings to disk. Caller must hold ``self._lock``."""
        data = {
            sid: str(ws)
            for sid, ws in self._workspaces.items()
        }
        with _PERSIST_LOCK:
            _BINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # A unique source prevents one manager from replacing another
            # manager's shared ``bindings.tmp`` before it can be committed.
            tmp = _BINDINGS_PATH.with_name(
                f".{_BINDINGS_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _atomic_replace_with_retry(tmp, _BINDINGS_PATH)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- binding --------------------------------------------------------------

    def set(self, session_id: str, path_str: str) -> Path:
        """Bind *session_id* to *path_str*, returning the resolved root.

        *path_str* is resolved immediately so that relative paths are
        anchored to the process's cwd at bind time.  The binding is
        persisted to disk and survives restarts.
        """
        resolved = Path(path_str).resolve()
        if not resolved.exists():
            raise WorkspaceError(f"路径不存在: \"{path_str}\"")
        if not resolved.is_dir():
            raise WorkspaceError(f"路径不是目录: \"{path_str}\"")
        with self._lock:
            self._workspaces[session_id] = resolved
            self._save()
        return resolved

    def get(self, session_id: str) -> Path | None:
        """Return the workspace root for *session_id*, or ``None``."""
        with self._lock:
            return self._workspaces.get(session_id)

    def unset(self, session_id: str) -> None:
        """Remove the workspace binding for *session_id*."""
        with self._lock:
            self._workspaces.pop(session_id, None)
            self._save()

    def require(self, session_id: str) -> Path:
        """Return the workspace root for *session_id*.

        Raises ``WorkspaceError`` if no workspace is bound to the session
        (unless unlimited mode is enabled, in which case the filesystem
        root is returned).
        """
        with self._lock:
            if session_id in self._unlimited_sessions:
                # In unlimited mode, use the filesystem root so no path is
                # considered "outside" the workspace.
                if os.name == "nt":
                    return Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\").resolve()
                return Path("/")
            ws = self._workspaces.get(session_id)
        if ws is None:
            raise WorkspaceError(
                f"session \"{session_id}\" 尚未设置 workspace。"
                f"请使用 /workspace set <路径> 进行设置。"
            )
        return ws

    # -- path resolution ------------------------------------------------------

    def resolve(
        self,
        session_id: str,
        path_str: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Safely resolve *path_str* within the workspace of *session_id*.

        Returns the absolute ``Path``.  Raises ``WorkspaceError`` when:

        - *session_id* has no workspace binding.
        - *path_str* is an absolute path.
        - *path_str* escapes the workspace boundary (e.g. ``../out.txt``).
        - *must_exist* is ``True`` and the path does not exist on disk.

        When unlimited mode is enabled for *session_id*, all boundary
        checks are bypassed and the path is resolved as-is.
        """
        # Atomically read both unlimited and workspace state to avoid
        # TOCTOU races with concurrent /unlimited toggles.
        with self._lock:
            unlimited = session_id in self._unlimited_sessions
            ws = self._workspaces.get(session_id)

        if unlimited:
            resolved = Path(path_str).resolve()
            if must_exist and not resolved.exists():
                raise WorkspaceError(f"路径不存在: \"{path_str}\"")
            return resolved

        if ws is None:
            raise WorkspaceError(
                f"session \"{session_id}\" 尚未设置 workspace。"
                f"请使用 /workspace set <路径> 进行设置。"
            )

        p = Path(path_str)

        # Reject absolute paths — only relative paths are allowed within
        # the workspace sandbox.
        if p.is_absolute():
            raise WorkspaceError(
                f"拒绝使用绝对路径: \"{path_str}\"。"
                f"请使用相对于 workspace 的路径。"
            )

        # Resolve the full path and check that it stays inside the
        # workspace root.
        resolved = (ws / p).resolve()

        try:
            resolved.relative_to(ws)
        except ValueError:
            raise WorkspaceError(
                f"路径超出 workspace 范围: \"{path_str}\"。"
                f"禁止使用 \"../\" 访问 workspace 之外的文件。"
            )

        if must_exist and not resolved.exists():
            raise WorkspaceError(f"路径不存在: \"{path_str}\"")

        return resolved
