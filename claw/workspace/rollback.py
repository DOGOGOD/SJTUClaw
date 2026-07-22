"""Workspace checkpoints and coordinated conversation rollback.

The checkpoint store is deliberately separate from the user's Git repository.
File contents are kept in a SHA-256 content-addressed object store while SQLite
stores bindings, immutable manifests, conversation snapshots, and operation
journal rows.  A checkpoint represents the state immediately *before* a user
turn, so restoring it also removes that turn and everything after it from the
materialized conversation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from claw.config import DATA_DIR
from claw.session.models import Session
from claw.session.store import SessionStore
from claw.utils import now_iso
from claw.workspace.manager import WorkspaceError, WorkspaceManager


_EXCLUDED_DIRS = frozenset({".git", ".hg", ".svn", ".sjtuclaw-rollback-tmp"})


class RollbackError(RuntimeError):
    """A safe, user-facing rollback failure."""


@dataclass(frozen=True)
class RollbackPreview:
    checkpoint_id: str
    message_preview: str
    restore_files: tuple[str, ...]
    delete_paths: tuple[str, ...]
    restore_directories: tuple[str, ...]
    messages_to_remove: int
    partial: bool = False

    def to_dict(self) -> dict:
        return {
            "checkpointId": self.checkpoint_id,
            "messagePreview": self.message_preview,
            "filesToRestore": len(self.restore_files),
            "filesToDelete": len(self.delete_paths),
            "directoriesToRestore": len(self.restore_directories),
            "messagesToRemove": self.messages_to_remove,
            "restoreFiles": list(self.restore_files),
            "deletePaths": list(self.delete_paths),
            "unlimitedWarning": self.partial,
        }


class WorkspaceRollbackManager:
    """Persistent workspace checkpoint manager.

    Locks are keyed by canonical workspace path, not session id, so two
    sessions bound to the same directory cannot interleave turns or restores.
    """

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        session_store: SessionStore,
        *,
        storage_root: Path | None = None,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.session_store = session_store
        self.storage_root = Path(storage_root or (DATA_DIR / "workspace" / "rollback"))
        self.objects_dir = self.storage_root / "objects"
        self.db_path = self.storage_root / "state.db"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self._meta_lock = threading.RLock()
        self._storage_lock = threading.RLock()
        self._workspace_locks: dict[str, threading.RLock] = {}
        self._session_locks: dict[str, threading.RLock] = {}
        self._init_db()
        self.recover_incomplete_operations()

    # -- database ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    session_id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    target_message_id TEXT,
                    message_preview TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    partial INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoints_session_created
                    ON checkpoints(session_id, created_at);
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    target_checkpoint_id TEXT NOT NULL,
                    safety_checkpoint_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )

    def recover_incomplete_operations(self) -> int:
        """Compensate rollback operations interrupted by process exit.

        The safety checkpoint is durable before any workspace path changes,
        therefore replaying it is idempotent and returns both files and the
        conversation to the pre-rollback state.
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE operations SET status='FAILED',error=?,completed_at=?
                   WHERE status IN ('PREPARED','FILES_APPLIED','COMPENSATING')
                     AND NOT EXISTS (
                       SELECT 1 FROM checkpoints c JOIN bindings b
                         ON b.session_id=operations.session_id
                        AND b.binding_id=c.binding_id
                       WHERE c.checkpoint_id=operations.safety_checkpoint_id
                     )""",
                ("Workspace binding 已变化，拒绝向新 workspace 恢复旧安全点。", now_iso()),
            )
            rows = conn.execute(
                """SELECT o.operation_id,o.session_id,o.safety_checkpoint_id,
                          c.manifest_json,c.session_json,c.binding_id,b.root_path
                   FROM operations o
                   JOIN checkpoints c ON c.checkpoint_id=o.safety_checkpoint_id
                   JOIN bindings b ON b.session_id=o.session_id
                                  AND b.binding_id=c.binding_id
                   WHERE o.status IN ('PREPARED','FILES_APPLIED','COMPENSATING')"""
            ).fetchall()
        recovered = 0
        for row in rows:
            try:
                with self.turn_guard(row["session_id"]):
                    self._apply_manifest(Path(row["root_path"]), json.loads(row["manifest_json"]))
                    live = self.session_store.get(row["session_id"])
                    self._restore_session(live, self._decode_session_snapshot(row["session_json"]))
                    self.session_store.save(live, fsync=True)
                status, error = "ROLLED_BACK", None
                recovered += 1
            except Exception as exc:  # best effort during startup
                status, error = "FAILED", str(exc)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE operations SET status=?,error=?,completed_at=? WHERE operation_id=?",
                    (status, error, now_iso(), row["operation_id"]),
                )
                if status == "ROLLED_BACK":
                    conn.execute(
                        "UPDATE checkpoints SET status='used' WHERE checkpoint_id=?",
                        (row["safety_checkpoint_id"],),
                    )
            if status == "ROLLED_BACK":
                self._prune_checkpoints(row["session_id"], row["binding_id"])
        return recovered

    # -- locking ----------------------------------------------------------

    def _get_lock(self, mapping: dict[str, threading.RLock], key: str) -> threading.RLock:
        with self._meta_lock:
            return mapping.setdefault(key, threading.RLock())

    @contextmanager
    def session_guard(self, session_id: str) -> Iterator[None]:
        session_lock = self._get_lock(self._session_locks, session_id)
        with session_lock:
            yield

    @contextmanager
    def turn_guard(self, session_id: str) -> Iterator[None]:
        # The session lock is acquired before reading the binding.  Workspace
        # set/unset uses the same lock, so a turn cannot silently switch roots
        # after its checkpoint has been captured.
        with self.session_guard(session_id):
            workspace = self.workspace_manager.get(session_id)
            if workspace is None:
                yield
                return
            root_key = os.path.normcase(str(workspace.resolve()))
            workspace_lock = self._get_lock(self._workspace_locks, root_key)
            with workspace_lock:
                yield

    # -- binding lifecycle ------------------------------------------------

    def enable(self, session_id: str, session: Session | None = None) -> dict:
        self._assert_no_incomplete_operation(session_id)
        workspace = self.workspace_manager.get(session_id)
        if workspace is None:
            raise RollbackError("当前 session 未设置 workspace。")
        root = str(workspace.resolve())
        with self.turn_guard(session_id), self._storage_lock, self._connect() as conn:
            old = conn.execute(
                "SELECT * FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            if old and old["root_path"] == root and old["enabled"]:
                return self.status(session_id)
            generation = int(old["generation"]) + 1 if old else 1
            binding_id = f"binding_{uuid.uuid4().hex}"
            conn.execute(
                """INSERT OR REPLACE INTO bindings
                   (session_id,binding_id,root_path,generation,enabled,created_at)
                   VALUES (?,?,?,?,1,?)""",
                (session_id, binding_id, root, generation, now_iso()),
            )
            live = session or self.session_store.get(session_id)
            self._insert_checkpoint(
                conn, session_id, binding_id, live,
                target_message_id=None, message_preview="Workspace 基线",
                kind="baseline", partial=False,
            )
        self._prune_checkpoints(session_id, binding_id)
        return self.status(session_id)

    def disable(self, session_id: str) -> None:
        with self.session_guard(session_id):
            self._assert_no_incomplete_operation(session_id)
            with self._connect() as conn:
                conn.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM operations WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM bindings WHERE session_id=?", (session_id,))
            self.garbage_collect()

    def _binding(self, session_id: str) -> sqlite3.Row | None:
        with self._storage_lock, self._connect() as conn:
            return conn.execute(
                "SELECT * FROM bindings WHERE session_id=? AND enabled=1", (session_id,)
            ).fetchone()

    def ensure_enabled(self, session_id: str, session: Session | None = None) -> sqlite3.Row:
        workspace = self.workspace_manager.get(session_id)
        if workspace is None:
            raise RollbackError("当前 session 未设置 workspace。请先使用 /workspace set <路径>。")
        binding = self._binding(session_id)
        if binding is None or Path(binding["root_path"]).resolve() != workspace.resolve():
            self.enable(session_id, session)
            binding = self._binding(session_id)
        if binding is None:
            raise RollbackError("Workspace 回退初始化失败。")
        return binding

    def status(self, session_id: str) -> dict:
        workspace = self.workspace_manager.get(session_id)
        binding = self._binding(session_id)
        binding_matches = (
            workspace is not None
            and binding is not None
            and Path(binding["root_path"]).resolve() == workspace.resolve()
        )
        if not binding_matches:
            return {
                "enabled": False,
                "workspace": str(workspace) if workspace else None,
                "checkpointCount": 0,
                "undoAvailable": False,
            }
        with self._connect() as conn:
            count = conn.execute(
                """SELECT COUNT(*) FROM checkpoints
                   WHERE session_id=? AND binding_id=?
                     AND kind='turn' AND status='active'""",
                (session_id, binding["binding_id"]),
            ).fetchone()[0]
            undo = conn.execute(
                """SELECT 1 FROM checkpoints WHERE session_id=? AND binding_id=?
                   AND kind='rollback_safety' AND status='active' LIMIT 1""",
                (session_id, binding["binding_id"]),
            ).fetchone() is not None
        return {
            "enabled": True,
            "workspace": str(workspace),
            "checkpointCount": int(count),
            "undoAvailable": undo,
            "bindingId": binding["binding_id"],
        }

    def active_turn_checkpoint_ids(self, session_id: str) -> set[str]:
        status = self.status(session_id)
        if not status["enabled"]:
            return set()
        binding = self._binding(session_id)
        if binding is None:
            return set()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT checkpoint_id FROM checkpoints
                   WHERE session_id=? AND binding_id=? AND kind='turn' AND status='active'""",
                (session_id, binding["binding_id"]),
            ).fetchall()
        return {str(row[0]) for row in rows}

    # -- capture ----------------------------------------------------------

    def create_turn_checkpoint(
        self,
        session_id: str,
        session: Session,
        *,
        message_id: str,
        message_preview: str,
        partial: bool = False,
    ) -> str | None:
        if self.workspace_manager.get(session_id) is None:
            return None
        self._assert_no_incomplete_operation(session_id)
        binding = self.ensure_enabled(session_id, session)
        with self._storage_lock, self._connect() as conn:
            # Undo is deliberately single-step.  Starting a new user turn
            # commits the current branch and invalidates the prior undo point.
            conn.execute(
                """UPDATE checkpoints SET status='used'
                   WHERE session_id=? AND binding_id=?
                     AND kind='rollback_safety' AND status='active'""",
                (session_id, binding["binding_id"]),
            )
            return self._insert_checkpoint(
                conn,
                session_id,
                binding["binding_id"],
                session,
                target_message_id=message_id,
                message_preview=message_preview,
                kind="turn",
                partial=partial,
            )

    def _insert_checkpoint(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        binding_id: str,
        session: Session,
        *,
        target_message_id: str | None,
        message_preview: str,
        kind: str,
        partial: bool,
    ) -> str:
        workspace = self.workspace_manager.get(session_id)
        if workspace is None:
            raise RollbackError("当前 session 未设置 workspace。")
        with self._storage_lock:
            manifest = self._scan_workspace(workspace, store_blobs=True)
            checkpoint_id = f"cp_{uuid.uuid4().hex}"
            parent = conn.execute(
                """SELECT checkpoint_id FROM checkpoints
                   WHERE session_id=? AND binding_id=? AND status='active'
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (session_id, binding_id),
            ).fetchone()
            session_json = self._encode_session_snapshot(session.to_snapshot_dict())
            conn.execute(
                """INSERT INTO checkpoints
                   (checkpoint_id,session_id,binding_id,parent_checkpoint_id,
                    target_message_id,message_preview,manifest_json,session_json,
                    kind,status,partial,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (
                    checkpoint_id, session_id, binding_id,
                    parent[0] if parent else None, target_message_id,
                    message_preview[:240], json.dumps(manifest, ensure_ascii=False),
                    session_json, kind, int(partial), now_iso(),
                ),
            )
        return checkpoint_id

    @staticmethod
    def _encode_session_snapshot(snapshot: dict) -> str:
        raw = json.dumps(snapshot, ensure_ascii=False, default=str).encode("utf-8")
        return "zlib:" + base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")

    @staticmethod
    def _decode_session_snapshot(value: str) -> dict:
        if value.startswith("zlib:"):
            raw = zlib.decompress(base64.b64decode(value[5:])).decode("utf-8")
            return json.loads(raw)
        return json.loads(value)

    def _prune_checkpoints(self, session_id: str, binding_id: str) -> None:
        """Remove unreachable branch metadata, then sweep unreferenced blobs."""
        with self._connect() as conn:
            conn.execute(
                """DELETE FROM checkpoints
                   WHERE session_id=? AND binding_id<>?
                     AND checkpoint_id NOT IN (
                       SELECT safety_checkpoint_id FROM operations
                       WHERE status IN ('PREPARED','FILES_APPLIED','COMPENSATING')
                     )""",
                (session_id, binding_id),
            )
            conn.execute(
                """DELETE FROM operations
                   WHERE session_id=?
                     AND status NOT IN ('PREPARED','FILES_APPLIED','COMPENSATING')
                     AND rowid NOT IN (
                       SELECT rowid FROM operations WHERE session_id=?
                       ORDER BY rowid DESC LIMIT 100
                     )""",
                (session_id, session_id),
            )
            conn.execute(
                """DELETE FROM checkpoints
                   WHERE session_id=? AND binding_id=?
                     AND status IN ('orphaned','used')
                     AND checkpoint_id NOT IN (
                       SELECT safety_checkpoint_id FROM operations
                       WHERE status IN ('PREPARED','FILES_APPLIED','COMPENSATING')
                     )""",
                (session_id, binding_id),
            )
        self.garbage_collect()

    def garbage_collect(self) -> int:
        """Mark-and-sweep content objects not referenced by any checkpoint."""
        with self._storage_lock:
            referenced: set[str] = set()
            with self._connect() as conn:
                rows = conn.execute("SELECT manifest_json FROM checkpoints").fetchall()
            for row in rows:
                try:
                    manifest = json.loads(row[0])
                except (TypeError, json.JSONDecodeError):
                    continue
                referenced.update(
                    entry["hash"]
                    for entry in manifest.values()
                    if entry.get("type") == "file" and entry.get("hash")
                )

            removed = 0
            if not self.objects_dir.exists():
                return removed
            for prefix_dir in self.objects_dir.iterdir():
                if not prefix_dir.is_dir():
                    continue
                for object_path in prefix_dir.iterdir():
                    digest = prefix_dir.name + object_path.name
                    if object_path.is_file() and len(digest) == 64 and digest not in referenced:
                        try:
                            object_path.unlink(missing_ok=True)
                            removed += 1
                        except OSError:
                            # Cleanup must never make checkpoint creation or
                            # workspace rebinding fail.
                            continue
                try:
                    prefix_dir.rmdir()
                except OSError:
                    pass
            return removed

    def _object_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RollbackError(f"无效的快照对象哈希: {digest!r}")
        return self.objects_dir / digest[:2] / digest[2:]

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _store_blob(self, path: Path) -> str:
        key = self._hash_file(path)
        destination = self._object_path(key)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(path, tmp)
                if self._hash_file(tmp) != key:
                    raise RollbackError(f"快照对象校验失败: {path}")
                os.replace(tmp, destination)
            finally:
                tmp.unlink(missing_ok=True)
        return key

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(info, "st_file_attributes", 0) & flag)

    def _scan_workspace(
        self, root: Path, *, store_blobs: bool = True
    ) -> dict[str, dict]:
        root = root.resolve()
        manifest: dict[str, dict] = {}

        def walk(directory: Path) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as exc:
                raise RollbackError(f"无法扫描 workspace: {directory}: {exc}") from exc
            for entry in entries:
                if entry.name in _EXCLUDED_DIRS:
                    continue
                path = Path(entry.path)
                try:
                    path.resolve(strict=False).relative_to(self.storage_root.resolve())
                    continue
                except ValueError:
                    pass
                rel = path.relative_to(root).as_posix()
                try:
                    info = entry.stat(follow_symlinks=False)
                    mode = stat.S_IMODE(info.st_mode)
                    if entry.is_symlink() or self._is_reparse_point(info):
                        manifest[rel] = {
                            "type": "symlink",
                            "target": os.readlink(path),
                            "mode": mode,
                            "directory": entry.is_dir(follow_symlinks=True),
                        }
                    elif entry.is_dir(follow_symlinks=False):
                        manifest[rel] = {"type": "directory", "mode": mode}
                        walk(path)
                    elif entry.is_file(follow_symlinks=False):
                        manifest[rel] = {
                            "type": "file",
                            "hash": (
                                self._store_blob(path)
                                if store_blobs else self._hash_file(path)
                            ),
                            "size": info.st_size, "mode": mode,
                        }
                except OSError as exc:
                    raise RollbackError(f"无法读取 workspace 路径: {rel}: {exc}") from exc

        walk(root)
        return manifest

    # -- query ------------------------------------------------------------

    def list_checkpoints(self, session_id: str) -> list[dict]:
        binding = self._binding(session_id)
        if binding is None:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT checkpoint_id,target_message_id,message_preview,kind,partial,created_at
                   FROM checkpoints WHERE session_id=? AND binding_id=? AND status='active'
                   ORDER BY created_at DESC, rowid DESC""",
                (session_id, binding["binding_id"]),
            ).fetchall()
        return [
            {
                "checkpointId": row["checkpoint_id"],
                "messageId": row["target_message_id"],
                "messagePreview": row["message_preview"],
                "kind": row["kind"],
                "partial": bool(row["partial"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def _resolve_checkpoint(self, session_id: str, target: str | int | None) -> sqlite3.Row:
        self._assert_no_incomplete_operation(session_id)
        binding = self.ensure_enabled(session_id)
        with self._connect() as conn:
            if target == "undo":
                row = conn.execute(
                    """SELECT * FROM checkpoints WHERE session_id=? AND binding_id=?
                       AND kind='rollback_safety' AND status='active'
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (session_id, binding["binding_id"]),
                ).fetchone()
            elif isinstance(target, str) and target.startswith("cp_"):
                row = conn.execute(
                    """SELECT * FROM checkpoints WHERE checkpoint_id=? AND session_id=?
                       AND binding_id=? AND status='active'""",
                    (target, session_id, binding["binding_id"]),
                ).fetchone()
            else:
                try:
                    steps = int(target or 1)
                except (TypeError, ValueError) as exc:
                    raise RollbackError(
                        "回退目标必须是正整数或 cp_ 开头的 checkpointId。"
                    ) from exc
                if steps < 1:
                    raise RollbackError("回退步数必须大于等于 1。")
                offset = steps - 1
                row = conn.execute(
                    """SELECT * FROM checkpoints WHERE session_id=? AND binding_id=?
                       AND kind='turn' AND status='active'
                       ORDER BY created_at DESC, rowid DESC LIMIT 1 OFFSET ?""",
                    (session_id, binding["binding_id"], offset),
                ).fetchone()
        if row is None:
            raise RollbackError("没有找到可用的回退点。")
        return row

    def _assert_no_incomplete_operation(self, session_id: str) -> None:
        with self._connect() as conn:
            pending = conn.execute(
                """SELECT 1 FROM operations WHERE session_id=?
                   AND status IN ('PREPARED','FILES_APPLIED','COMPENSATING') LIMIT 1""",
                (session_id,),
            ).fetchone()
        if pending is not None:
            raise RollbackError("存在尚未完成的回退补偿，请重启服务以自动恢复。")

    def preview(self, session_id: str, target: str | int | None = None) -> RollbackPreview:
        with self.turn_guard(session_id):
            row = self._resolve_checkpoint(session_id, target)
            workspace = self.workspace_manager.get(session_id)
            if workspace is None:
                raise RollbackError("当前 session 未设置 workspace。")
            current = self._scan_workspace(workspace, store_blobs=False)
            wanted = json.loads(row["manifest_json"])
            restore_files = tuple(sorted(
                path for path, entry in wanted.items()
                if entry["type"] in ("file", "symlink") and current.get(path) != entry
            ))
            restore_dirs = tuple(sorted(
                path for path, entry in wanted.items()
                if entry["type"] == "directory" and current.get(path) != entry
            ))
            delete_paths = tuple(sorted(set(current) - set(wanted), reverse=True))
            snapshot = self._decode_session_snapshot(row["session_json"])
            live = self.session_store.get(session_id)
            current_messages = len(live.messages)
            old_messages = len(snapshot.get("messages", []))
            partial = self._restore_is_partial(live, snapshot, row)
            return RollbackPreview(
                checkpoint_id=row["checkpoint_id"],
                message_preview=row["message_preview"],
                restore_files=restore_files,
                delete_paths=delete_paths,
                restore_directories=restore_dirs,
                messages_to_remove=max(0, current_messages - old_messages),
                partial=partial,
            )

    # -- restore ----------------------------------------------------------

    def _restore_is_partial(
        self, live: Session, snapshot: dict, target_row: sqlite3.Row
    ) -> bool:
        if bool(target_row["partial"]):
            return True
        old_count = min(len(live.messages), len(snapshot.get("messages", [])))
        checkpoint_ids = {
            message.rollback_checkpoint_id
            for message in live.messages[old_count:]
            if message.rollback_checkpoint_id
        }
        if not checkpoint_ids:
            return False
        placeholders = ",".join("?" for _ in checkpoint_ids)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM checkpoints WHERE checkpoint_id IN ({placeholders}) AND partial=1 LIMIT 1",
                tuple(checkpoint_ids),
            ).fetchone()
        return row is not None

    def rollback(self, session_id: str, target: str | int | None = None) -> dict:
        with self.turn_guard(session_id):
            row = self._resolve_checkpoint(session_id, target)
            binding = self._binding(session_id)
            if binding is None:
                raise RollbackError("当前 session 未启用 workspace 回退。")
            live = self.session_store.get(session_id)
            target_snapshot = self._decode_session_snapshot(row["session_json"])
            partial = self._restore_is_partial(live, target_snapshot, row)
            operation_id = f"rb_{uuid.uuid4().hex}"
            safety_kind = (
                "operation_safety" if row["kind"] == "rollback_safety"
                else "rollback_safety"
            )
            with self._storage_lock, self._connect() as conn:
                safety_id = self._insert_checkpoint(
                    conn, session_id, binding["binding_id"], live,
                    target_message_id=None, message_preview="回退前安全点",
                    kind=safety_kind, partial=False,
                )
                conn.execute(
                    """INSERT INTO operations
                       (operation_id,session_id,target_checkpoint_id,safety_checkpoint_id,
                        status,created_at) VALUES (?,?,?,?,?,?)""",
                    (operation_id, session_id, row["checkpoint_id"], safety_id, "PREPARED", now_iso()),
                )

            wanted = json.loads(row["manifest_json"])
            try:
                restored, deleted = self._apply_manifest(Path(binding["root_path"]), wanted)
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE operations SET status='FILES_APPLIED' WHERE operation_id=?",
                        (operation_id,),
                    )
                self._restore_session(live, target_snapshot)
                self.session_store.save(live, fsync=True)
            except Exception as exc:
                # Restore the pre-rollback safety state.  The conversation was
                # not persisted until after the workspace succeeded.
                with self._connect() as conn:
                    safety = conn.execute(
                        "SELECT manifest_json,session_json FROM checkpoints WHERE checkpoint_id=?",
                        (safety_id,),
                    ).fetchone()
                    conn.execute(
                        "UPDATE operations SET status='COMPENSATING',error=? WHERE operation_id=?",
                        (str(exc), operation_id),
                    )
                if safety:
                    try:
                        self._apply_manifest(Path(binding["root_path"]), json.loads(safety[0]))
                        self._restore_session(live, self._decode_session_snapshot(safety[1]))
                        self.session_store.save(live, fsync=True)
                    except Exception as compensation_exc:
                        raise RollbackError(
                            f"回退失败且安全点补偿尚未完成；重启后将自动重试: {compensation_exc}"
                        ) from exc
                    with self._connect() as conn:
                        conn.execute(
                            """UPDATE operations SET status='COMPENSATED',
                               completed_at=? WHERE operation_id=?""",
                            (now_iso(), operation_id),
                        )
                        conn.execute(
                            "UPDATE checkpoints SET status='used' WHERE checkpoint_id=?",
                            (safety_id,),
                        )
                    self._prune_checkpoints(session_id, binding["binding_id"])
                raise RollbackError(f"回退失败，已恢复回退前状态: {exc}") from exc

            with self._connect() as conn:
                conn.execute(
                    "UPDATE operations SET status='COMMITTED',completed_at=? WHERE operation_id=?",
                    (now_iso(), operation_id),
                )
                active_turn_checkpoints = {
                    message.rollback_checkpoint_id
                    for message in live.messages
                    if message.rollback_checkpoint_id
                }
                conn.execute(
                    """UPDATE checkpoints SET status='orphaned'
                       WHERE session_id=? AND binding_id=? AND kind='turn'""",
                    (session_id, binding["binding_id"]),
                )
                if active_turn_checkpoints:
                    placeholders = ",".join("?" for _ in active_turn_checkpoints)
                    conn.execute(
                        f"UPDATE checkpoints SET status='active' WHERE checkpoint_id IN ({placeholders})",
                        tuple(active_turn_checkpoints),
                    )
                if row["kind"] == "rollback_safety":
                    conn.execute("UPDATE checkpoints SET status='used' WHERE checkpoint_id=?", (row["checkpoint_id"],))
                    conn.execute(
                        "UPDATE checkpoints SET status='used' WHERE checkpoint_id=?",
                        (safety_id,),
                    )
                else:
                    conn.execute(
                        """UPDATE checkpoints SET status='used'
                           WHERE session_id=? AND binding_id=?
                             AND kind='rollback_safety' AND checkpoint_id<>?""",
                        (session_id, binding["binding_id"], safety_id),
                    )
            self._prune_checkpoints(session_id, binding["binding_id"])
            return {
                "checkpointId": row["checkpoint_id"],
                "restored": restored,
                "deleted": deleted,
                "messages": [message.to_dict() for message in live.messages],
                "undoAvailable": True,
                "partial": partial,
            }

    def undo(self, session_id: str) -> dict:
        return self.rollback(session_id, "undo")

    def _restore_session(self, live: Session, snapshot: dict) -> None:
        restored = Session.from_dict(snapshot)
        next_revision = live.revision + 1
        live.title = restored.title
        live.messages = restored.messages
        live.summary = restored.summary
        live.skill_usage = restored.skill_usage
        live.created_at = restored.created_at
        live.updated_at = now_iso()
        live.last_consolidated = restored.last_consolidated
        live.metadata = restored.metadata
        # Pi sessions are append-only.  After restoring an older SJTUClaw
        # conversation, start a fresh Pi branch so removed turns cannot leak
        # back into subsequent prompts.  Undo rotates again for the same reason.
        live.metadata["pi_session_generation"] = uuid.uuid4().hex
        live.metadata.pop("runtime_checkpoint", None)
        live.metadata.pop("pending_user_turn", None)
        live.revision = next_revision

    def _safe_target(self, root: Path, relative: str) -> Path:
        rel = Path(relative)
        if (
            rel.is_absolute()
            or bool(rel.drive)
            or any(part in ("", ".", "..") for part in rel.parts)
        ):
            raise RollbackError(f"快照包含越界路径: {relative}")
        # Do not resolve the final target: an existing symlink may point
        # outside the workspace and must still be removable as a link.
        return root.resolve() / rel

    def _path_is_link_like(self, path: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(info.st_mode) or self._is_reparse_point(info)

    def _remove_path(self, target: Path) -> None:
        if self._path_is_link_like(target):
            try:
                target.unlink()
            except (IsADirectoryError, PermissionError):
                os.rmdir(target)
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def _assert_real_parents(self, root: Path, target: Path) -> None:
        relative_parent = target.parent.relative_to(root)
        current = root
        for part in relative_parent.parts:
            current = current / part
            if self._path_is_link_like(current):
                raise RollbackError(f"拒绝通过目录链接恢复 workspace 路径: {current}")

    def _apply_manifest(self, root: Path, wanted: dict[str, dict]) -> tuple[int, int]:
        root = root.resolve()
        current = self._scan_workspace(root, store_blobs=False)
        deleted = 0
        restored = 0

        # Remove deepest extra paths first.
        extras = sorted(set(current) - set(wanted), key=lambda value: (value.count("/"), value), reverse=True)
        for relative in extras:
            target = self._safe_target(root, relative)
            self._remove_path(target)
            deleted += 1

        # Directories first, then files/symlinks.
        for relative, entry in sorted(wanted.items(), key=lambda item: (item[0].count("/"), item[0])):
            if entry["type"] != "directory":
                continue
            target = self._safe_target(root, relative)
            self._assert_real_parents(root, target)
            if self._path_is_link_like(target) or (target.exists() and not target.is_dir()):
                self._remove_path(target)
            target.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(target, entry["mode"])

        for relative, entry in sorted(wanted.items()):
            if current.get(relative) == entry:
                continue
            target = self._safe_target(root, relative)
            if entry["type"] == "directory":
                continue
            self._assert_real_parents(root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or self._path_is_link_like(target):
                self._remove_path(target)
            if entry["type"] == "symlink":
                os.symlink(
                    entry["target"],
                    target,
                    target_is_directory=bool(entry.get("directory", False)),
                )
            elif entry["type"] == "file":
                source = self._object_path(entry["hash"])
                if not source.exists():
                    raise RollbackError(f"快照对象丢失: {entry['hash']}")
                tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.rollback.tmp")
                shutil.copyfile(source, tmp)
                if self._hash_file(tmp) != entry["hash"]:
                    tmp.unlink(missing_ok=True)
                    raise RollbackError(f"恢复文件校验失败: {relative}")
                os.replace(tmp, target)
                if os.name != "nt":
                    os.chmod(target, entry["mode"])
            restored += 1
        return restored, deleted
