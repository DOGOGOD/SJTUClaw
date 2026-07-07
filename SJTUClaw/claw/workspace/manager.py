"""Workspace manager.

Design choice: workspace is **per-session**. Each session can have its
own workspace bound independently. The binding is persisted in
``data/workspace/<sessionId>.json`` so it survives restarts.

When no workspace is set for a session, any tool that modifies the
filesystem (update tools, shell tools, attachment copy, download
creation) must fail with a clear error asking the user to set a
workspace first.

Path resolution:
    - Relative paths are resolved against the workspace root.
    - ``..`` escapes and absolute paths that land outside the workspace
      are rejected.
    - The resolved path is returned as an absolute ``pathlib.Path``.

This module is intentionally decoupled from the tool registry and the
agent loop — it only owns the workspace mapping and path safety logic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

from claw.config import DATA_DIR

_WORKSPACE_DIR = DATA_DIR / "workspace"
_WORKSPACE_FILE_NAME = "workspace.json"


class WorkspaceError(RuntimeError):
    """Raised for workspace-related configuration or boundary violations."""


class WorkspaceManager:
    """Manage per-session workspace bindings.

    Usage::

        mgr = WorkspaceManager()
        mgr.set(session_id, "/home/user/project")
        resolved = mgr.resolve(session_id, "src/main.py")  # -> Path

    Thread-compatible but not thread-safe — all callers are expected to
    be serialised by the agent loop for a given session.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}
        self._load_all()

    def set(self, session_id: str, path: str | Path) -> Path:
        """Bind *session_id* to *path* as its workspace.

        *path* must be an existing directory. Returns the resolved
        absolute ``Path``.
        """
        resolved = Path(path).resolve()
        if not resolved.exists():
            raise WorkspaceError(f"路径不存在：{resolved}")
        if not resolved.is_dir():
            raise WorkspaceError(f"路径不是目录：{resolved}")
        self._cache[session_id] = str(resolved)
        self._persist(session_id)
        return resolved

    def unset(self, session_id: str) -> None:
        """Remove the workspace binding for *session_id*."""
        self._cache.pop(session_id, None)
        self._persist(session_id)

    def get(self, session_id: str) -> Path | None:
        """Return the workspace root for *session_id* or None if unset."""
        raw = self._cache.get(session_id)
        return Path(raw) if raw else None

    def is_set(self, session_id: str) -> bool:
        """Return True if *session_id* has a workspace bound."""
        return session_id in self._cache

    def require(self, session_id: str) -> Path:
        """Like ``get()`` but raises ``WorkspaceError`` if unset.

        Call this from every tool that needs a workspace.
        """
        ws = self.get(session_id)
        if ws is None:
            raise WorkspaceError(
                "当前 session 未设置 workspace。"
                "请先使用 /workspace set <路径> 设置 workspace，"
                "然后再执行需要文件系统操作的工具调用。"
            )
        return ws

    # ------------------------------------------------------------------
    # Path resolution & boundary enforcement
    # ------------------------------------------------------------------

    def resolve(
        self,
        session_id: str,
        rel_path: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve *rel_path* against the workspace root for *session_id*.

        Rules:
        1. Workspace must be set (raises ``WorkspaceError`` otherwise).
        2. The caller-provided path is joined with the workspace root.
        3. ``..`` traversals and absolute paths that land outside the
           workspace are rejected — the final resolved path must start
           with the workspace root.
        4. If *must_exist* is True, the resolved path must already exist
           on the filesystem (used by tools that read or modify an
           existing file).

        Returns the resolved absolute ``Path``.
        """
        ws = self.require(session_id)

        # Reject empty paths early
        if not rel_path or not rel_path.strip():
            raise WorkspaceError("路径不能为空")

        # Resolve and check boundary
        resolved = (ws / rel_path).resolve()

        # Ensure the resolved path is within the workspace
        ws_str = str(ws)
        resolved_str = str(resolved)
        if not resolved_str.startswith(ws_str + os.sep) and resolved_str != ws_str:
            raise WorkspaceError(
                f"路径越界：\"{rel_path}\" 的解析结果 \"{resolved}\" 不在 "
                f"workspace \"{ws}\" 内。不允许通过 ../ 或绝对路径逃逸 workspace。"
            )

        if must_exist and not resolved.exists():
            raise WorkspaceError(f"路径不存在于 workspace 中：\"{rel_path}\"")

        return resolved

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _workspace_file(self, session_id: str) -> Path:
        return _WORKSPACE_DIR / session_id / _WORKSPACE_FILE_NAME

    def _persist(self, session_id: str) -> None:
        """Write the cached workspace path to disk."""
        path = self._cache.get(session_id)
        ws_file = self._workspace_file(session_id)
        if path is None:
            # Unset: remove the file if it exists
            try:
                if ws_file.exists():
                    ws_file.unlink()
                parent = ws_file.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
            return

        try:
            ws_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = ws_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"sessionId": session_id, "workspace": str(path)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(ws_file)
        except OSError as exc:
            raise WorkspaceError(f"保存 workspace 配置失败: {exc}") from exc

    def _load_all(self) -> None:
        """Load all persisted workspace bindings into the cache."""
        self._cache.clear()
        if not _WORKSPACE_DIR.exists():
            return

        for entry in sorted(_WORKSPACE_DIR.iterdir()):
            if not entry.is_dir():
                continue
            ws_file = entry / _WORKSPACE_FILE_NAME
            if not ws_file.exists():
                continue
            try:
                data = json.loads(ws_file.read_text(encoding="utf-8"))
                sid = data.get("sessionId")
                ws_path = data.get("workspace")
                if sid and ws_path and Path(ws_path).is_dir():
                    self._cache[sid] = ws_path
            except (OSError, json.JSONDecodeError, KeyError):
                continue
