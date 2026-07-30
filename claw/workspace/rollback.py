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
import time
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from claw.config import DATA_DIR
from claw.env_utils import env_float, env_int
from claw.session.models import Session
from claw.session.store import (
    AUTO_MODE_METADATA_KEY,
    SANDBOX_MODE_METADATA_KEY,
    SessionStore,
)
from claw.utils import now_iso
from claw.workspace.manager import WorkspaceManager


_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".sjtuclaw-rollback-tmp",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".venv-build",
        "__pycache__",
        "node_modules",
    }
)

_MANIFEST_VERSION = 2
_DEFAULT_MAX_FILES = 100_000
_DEFAULT_MAX_FILE_BYTES = 128 * 1024 * 1024
_DEFAULT_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_DEFAULT_SCAN_TIMEOUT_S = 5.0
_DEFAULT_SCAN_WORKERS = 4
_ORPHAN_CAPTURE_MAX_AGE_S = 24 * 60 * 60


class RollbackError(RuntimeError):
    """A safe, user-facing rollback failure."""


@dataclass(frozen=True)
class WorkspaceScan:
    """A bounded workspace scan plus coverage information."""

    entries: dict[str, dict]
    ignored_paths: tuple[str, ...] = ()
    complete: bool = True
    warnings: tuple[str, ...] = ()
    stats: dict[str, int | float] | None = None

    @property
    def partial(self) -> bool:
        return not self.complete or bool(self.ignored_paths)

    def to_payload(self) -> dict:
        return {
            "__sjtuclawManifestVersion": _MANIFEST_VERSION,
            "entries": self.entries,
            "ignoredPaths": list(self.ignored_paths),
            "complete": self.complete,
            "warnings": list(self.warnings),
            "stats": self.stats or {},
        }


@dataclass(frozen=True)
class RollbackPreview:
    checkpoint_id: str
    message_preview: str
    restore_files: tuple[str, ...]
    delete_paths: tuple[str, ...]
    restore_directories: tuple[str, ...]
    messages_to_remove: int
    partial: bool = False
    warnings: tuple[str, ...] = ()

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
            "partial": self.partial,
            "warnings": list(self.warnings),
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
        max_files: int | None = None,
        max_file_bytes: int | None = None,
        max_snapshot_bytes: int | None = None,
        scan_timeout_s: float | None = None,
        scan_workers: int | None = None,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.session_store = session_store
        self.storage_root = Path(storage_root or (DATA_DIR / "workspace" / "rollback"))
        self.objects_dir = self.storage_root / "objects"
        self.db_path = self.storage_root / "state.db"
        self.max_files = max_files if max_files is not None else env_int(
            "ROLLBACK_MAX_FILES", _DEFAULT_MAX_FILES, minimum=1
        )
        self.max_file_bytes = (
            max_file_bytes if max_file_bytes is not None else env_int(
                "ROLLBACK_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES, minimum=1
            )
        )
        self.max_snapshot_bytes = (
            max_snapshot_bytes if max_snapshot_bytes is not None else env_int(
                "ROLLBACK_MAX_SNAPSHOT_BYTES",
                _DEFAULT_MAX_SNAPSHOT_BYTES,
                minimum=1,
            )
        )
        self.scan_timeout_s = (
            scan_timeout_s if scan_timeout_s is not None else env_float(
                "ROLLBACK_SCAN_TIMEOUT_S",
                _DEFAULT_SCAN_TIMEOUT_S,
                minimum=0.1,
            )
        )
        self.scan_workers = scan_workers if scan_workers is not None else env_int(
            "ROLLBACK_SCAN_WORKERS",
            _DEFAULT_SCAN_WORKERS,
            minimum=1,
            maximum=16,
        )
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self._meta_lock = threading.RLock()
        self._storage_lock = threading.RLock()
        self._workspace_locks: dict[str, threading.RLock] = {}
        self._session_locks: dict[str, threading.RLock] = {}
        self._init_db()
        self.recover_incomplete_operations()

    # -- database ---------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection and always release its handle."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
                CREATE TABLE IF NOT EXISTS preferences (
                    session_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    explicit INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_cache (
                    root_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (root_path,relative_path)
                );
                """
            )
            preference_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(preferences)").fetchall()
            }
            if "explicit" not in preference_columns:
                # Existing releases enabled rollback implicitly while setting
                # a Workspace.  Mark those rows as unverified so upgrading
                # requires a fresh, explicit `/rollback on`.
                conn.execute(
                    """ALTER TABLE preferences
                       ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0"""
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

    def preference(self, session_id: str) -> bool | None:
        """Return the persisted per-session rollback preference, if explicit."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled,explicit FROM preferences WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None or not bool(row["explicit"]):
            return None
        return bool(row["enabled"])

    def enable(
        self,
        session_id: str,
        session: Session | None = None,
        *,
        explicit: bool = True,
    ) -> dict:
        """Enable rollback metadata without snapshotting the workspace.

        A restorable checkpoint is captured immediately before each user turn
        by :meth:`create_turn_checkpoint`.  Capturing another, otherwise
        unused, baseline here made binding a large workspace copy every file
        before the UI could close its setup dialog.

        ``explicit`` is true only for `/rollback on`. Workspace rebinding may
        preserve an already explicit preference, but can never create one.
        """
        self._assert_no_incomplete_operation(session_id)
        workspace = self.workspace_manager.get(session_id)
        if workspace is None:
            raise RollbackError("当前 session 未设置 workspace。")
        root = str(workspace.resolve())
        with self.turn_guard(session_id), self._storage_lock, self._connect() as conn:
            old = conn.execute(
                "SELECT * FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            preference = conn.execute(
                "SELECT enabled,explicit FROM preferences WHERE session_id=?",
                (session_id,),
            ).fetchone()
            was_explicitly_enabled = bool(
                preference
                and preference["enabled"]
                and preference["explicit"]
            )
            if not explicit and not was_explicitly_enabled:
                raise RollbackError(
                    "当前 session 的 rollback 尚未显式开启。请使用 /rollback on 开启。"
                )
            if (
                old
                and old["root_path"] == root
                and old["enabled"]
                and was_explicitly_enabled
            ):
                binding_id = str(old["binding_id"])
            else:
                generation = int(old["generation"]) + 1 if old else 1
                binding_id = f"binding_{uuid.uuid4().hex}"
                conn.execute(
                    """INSERT OR REPLACE INTO bindings
                       (session_id,binding_id,root_path,generation,enabled,created_at)
                       VALUES (?,?,?,?,1,?)""",
                    (session_id, binding_id, root, generation, now_iso()),
                )
            if explicit:
                conn.execute(
                    """INSERT OR REPLACE INTO preferences
                       (session_id,enabled,explicit,updated_at)
                       VALUES (?,1,1,?)""",
                    (session_id, now_iso()),
                )
            else:
                conn.execute(
                    """UPDATE preferences SET enabled=1,updated_at=?
                       WHERE session_id=? AND explicit=1""",
                    (now_iso(), session_id),
                )
        self._prune_checkpoints(session_id, binding_id)
        return self.status(session_id)

    def disable(self, session_id: str) -> None:
        """Disable rollback until explicitly re-enabled for this session."""
        with self.session_guard(session_id):
            self._assert_no_incomplete_operation(session_id)
            with self._connect() as conn:
                conn.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM operations WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM bindings WHERE session_id=?", (session_id,))
                conn.execute(
                    """INSERT OR REPLACE INTO preferences
                       (session_id,enabled,explicit,updated_at)
                       VALUES (?,0,1,?)""",
                    (session_id, now_iso()),
                )
            self.garbage_collect()

    def purge(self, session_id: str) -> None:
        """Remove all rollback data and preferences for a removed workspace/session."""
        with self.session_guard(session_id):
            self._assert_no_incomplete_operation(session_id)
            with self._connect() as conn:
                conn.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM operations WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM bindings WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM preferences WHERE session_id=?", (session_id,))
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
        if self.preference(session_id) is not True:
            raise RollbackError(
                "当前 session 的 rollback 尚未开启。请使用 /rollback on 开启。"
            )
        binding = self._binding(session_id)
        if binding is None or Path(binding["root_path"]).resolve() != workspace.resolve():
            self.enable(session_id, session, explicit=False)
            binding = self._binding(session_id)
        if binding is None:
            raise RollbackError("Workspace 回退初始化失败。")
        return binding

    def status(self, session_id: str) -> dict:
        workspace = self.workspace_manager.get(session_id)
        binding = self._binding(session_id)
        preference = self.preference(session_id)
        binding_matches = (
            workspace is not None
            and binding is not None
            and preference is True
            and Path(binding["root_path"]).resolve() == workspace.resolve()
        )
        if not binding_matches:
            return {
                "enabled": False,
                "workspace": str(workspace) if workspace else None,
                "checkpointCount": 0,
                "partialCheckpointCount": 0,
                "undoAvailable": False,
                "preference": preference,
            }
        with self._connect() as conn:
            count = conn.execute(
                """SELECT COUNT(*) FROM checkpoints
                   WHERE session_id=? AND binding_id=?
                     AND kind='turn' AND status='active'""",
                (session_id, binding["binding_id"]),
            ).fetchone()[0]
            partial_count = conn.execute(
                """SELECT COUNT(*) FROM checkpoints
                   WHERE session_id=? AND binding_id=?
                     AND kind='turn' AND status='active' AND partial=1""",
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
            "partialCheckpointCount": int(partial_count),
            "undoAvailable": undo,
            "bindingId": binding["binding_id"],
            "preference": preference,
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
        if self.preference(session_id) is not True:
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
            # Acquire SQLite's cross-process writer lock before publishing
            # content objects.  Garbage collection uses the same lock, so it
            # cannot sweep a newly captured object before its manifest row is
            # committed.
            conn.execute(
                """UPDATE bindings SET generation=generation
                   WHERE session_id=? AND binding_id=?""",
                (session_id, binding_id),
            )
            scan = self._scan_workspace_report(
                workspace,
                store_blobs=True,
                cache_conn=conn,
            )
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
                    message_preview[:240],
                    json.dumps(scan.to_payload(), ensure_ascii=False),
                    session_json, kind, int(partial or scan.partial), now_iso(),
                ),
            )
        return checkpoint_id

    @staticmethod
    def _decode_manifest(value: str | dict) -> WorkspaceScan:
        payload = json.loads(value) if isinstance(value, str) else value
        version = (
            payload.get("__sjtuclawManifestVersion")
            if isinstance(payload, dict) else None
        )
        if (
            isinstance(payload, dict)
            and version == _MANIFEST_VERSION
            and isinstance(payload.get("entries"), dict)
        ):
            return WorkspaceScan(
                entries=payload["entries"],
                ignored_paths=tuple(
                    path for path in payload.get("ignoredPaths", [])
                    if isinstance(path, str)
                ),
                complete=bool(payload.get("complete", True)),
                warnings=tuple(
                    warning for warning in payload.get("warnings", [])
                    if isinstance(warning, str)
                ),
                stats=payload.get("stats") if isinstance(payload.get("stats"), dict) else {},
            )
        if version is not None:
            raise RollbackError(f"不支持的回退快照清单版本: {version}")
        if not isinstance(payload, dict):
            raise RollbackError("回退快照清单格式无效。")
        # Version 1 stored the entries dictionary directly.
        return WorkspaceScan(entries=payload)

    @staticmethod
    def _path_is_ignored(relative: str, ignored_paths: set[str]) -> bool:
        current = relative
        while current:
            if current in ignored_paths:
                return True
            current = current.rpartition("/")[0]
        return False

    @staticmethod
    def _path_contains_ignored(relative: str, ignored_paths: set[str]) -> bool:
        prefix = relative + "/"
        return any(path == relative or path.startswith(prefix) for path in ignored_paths)

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
            # BEGIN IMMEDIATE coordinates with checkpoint writers in other
            # processes.  Keep the transaction open through the file sweep so
            # the reference set and object directory form one stable view.
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT manifest_json FROM checkpoints"
                ).fetchall()

                removed = 0
                if self.objects_dir.exists():
                    # A process interruption can leave an unfinished capture
                    # next to the content-addressed object directories.  A
                    # long grace period also protects older clients that do
                    # not acquire the database writer lock before capture.
                    stale_before = time.time() - _ORPHAN_CAPTURE_MAX_AGE_S
                    for temp_path in self.objects_dir.glob(".capture.*.tmp"):
                        if not temp_path.is_file():
                            continue
                        try:
                            if temp_path.stat().st_mtime > stale_before:
                                continue
                            temp_path.unlink(missing_ok=True)
                            removed += 1
                        except OSError:
                            continue

                referenced: set[str] = set()
                for row in rows:
                    try:
                        manifest = self._decode_manifest(row[0]).entries
                    except (TypeError, json.JSONDecodeError, RollbackError):
                        continue
                    referenced.update(
                        entry["hash"]
                        for entry in manifest.values()
                        if entry.get("type") == "file" and entry.get("hash")
                    )

                if not self.objects_dir.exists():
                    return removed
                for prefix_dir in self.objects_dir.iterdir():
                    if not prefix_dir.is_dir():
                        continue
                    for object_path in prefix_dir.iterdir():
                        digest = prefix_dir.name + object_path.name
                        if (
                            object_path.is_file()
                            and len(digest) == 64
                            and digest not in referenced
                        ):
                            try:
                                object_path.unlink(missing_ok=True)
                                removed += 1
                            except OSError:
                                # Cleanup must never make checkpoint creation
                                # or workspace rebinding fail.
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

    def _object_has_size(self, digest: str, expected_size: int) -> bool:
        try:
            return self._object_path(digest).stat().st_size == expected_size
        except OSError:
            return False

    @staticmethod
    def _file_fingerprint(info: os.stat_result) -> tuple[int, int, int]:
        return (
            int(info.st_size),
            int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
            # Windows directory-entry ctime can lag behind fstat until the
            # file is reopened. Including it causes stable files to look as
            # if they changed during capture. Size + nanosecond mtime remain
            # the reliable content fingerprint on that platform.
            0 if os.name == "nt" else int(
                getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))
            ),
        )

    @classmethod
    def _opened_file_matches(
        cls,
        opened: os.stat_result,
        expected: os.stat_result,
    ) -> bool:
        if not stat.S_ISREG(opened.st_mode):
            return False
        if cls._file_fingerprint(opened) != cls._file_fingerprint(expected):
            return False
        # DirEntry inode is unavailable on some Windows Python builds (0).
        # Where both sides expose identity, require it to match as well.
        if expected.st_ino and opened.st_ino:
            return (
                expected.st_ino == opened.st_ino
                and expected.st_dev == opened.st_dev
            )
        return True

    @classmethod
    def _hash_file(
        cls,
        path: Path,
        *,
        expected: os.stat_result | None = None,
        deadline: float | None = None,
    ) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            opened = os.fstat(source.fileno())
            if expected is not None and not cls._opened_file_matches(opened, expected):
                raise OSError("文件在快照读取前发生变化")
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("workspace 快照扫描超时")
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if cls._file_fingerprint(os.fstat(source.fileno())) != cls._file_fingerprint(opened):
                raise OSError("文件在快照读取期间发生变化")
        return digest.hexdigest()

    def _store_blob(
        self,
        path: Path,
        *,
        expected: os.stat_result | None = None,
        deadline: float | None = None,
    ) -> str:
        """Capture and hash a file in one pass, then atomically publish it."""
        tmp = self.objects_dir / f".capture.{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as source, open(tmp, "xb") as destination_file:
                opened = os.fstat(source.fileno())
                if (
                    expected is not None
                    and not self._opened_file_matches(opened, expected)
                ):
                    raise OSError("文件在快照读取前发生变化")
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("workspace 快照扫描超时")
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    destination_file.write(chunk)
                if self._file_fingerprint(os.fstat(source.fileno())) != self._file_fingerprint(opened):
                    raise OSError("文件在快照读取期间发生变化")
                destination_file.flush()
            key = digest.hexdigest()
            destination = self._object_path(key)
            if destination.exists():
                try:
                    if destination.stat().st_size == tmp.stat().st_size:
                        return key
                except OSError:
                    pass
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(tmp, destination)
            except FileExistsError:
                # Another session may have published the same content.
                pass
            return key
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _copy_blob_verified(source: Path, destination: Path, expected_hash: str) -> None:
        """Copy an object while verifying it, without a second full read."""
        digest = hashlib.sha256()
        with open(source, "rb") as source_file, open(destination, "xb") as output:
            while True:
                chunk = source_file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
            output.flush()
        if digest.hexdigest() != expected_hash:
            destination.unlink(missing_ok=True)
            raise RollbackError(f"快照对象校验失败: {expected_hash}")

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(info, "st_file_attributes", 0) & flag)

    def _scan_workspace(
        self, root: Path, *, store_blobs: bool = True
    ) -> dict[str, dict]:
        """Compatibility wrapper returning only tracked manifest entries."""
        return self._scan_workspace_report(root, store_blobs=store_blobs).entries

    def _scan_workspace_report(
        self,
        root: Path,
        *,
        store_blobs: bool = True,
        cache_conn: sqlite3.Connection | None = None,
    ) -> WorkspaceScan:
        root = root.resolve()
        manifest: dict[str, dict] = {}
        ignored: list[str] = []
        seen_files: set[str] = set()
        warnings: list[str] = []
        cache_updates: list[tuple[str, str, int, int, int, str, str]] = []
        pending_files: list[
            tuple[str, Path, os.stat_result, int, tuple[int, int, int]]
        ] = []
        root_key = os.path.normcase(str(root))
        started = time.monotonic()
        deadline = started + self.scan_timeout_s
        files_seen = 0
        files_reused = 0
        bytes_read = 0
        oversized_count = 0
        budget_count = 0
        unstable_count = 0
        complete = True
        stopped_reason = ""

        try:
            storage_relative = self.storage_root.resolve().relative_to(root).as_posix()
        except ValueError:
            storage_relative = ""

        if cache_conn is not None:
            cached_rows = cache_conn.execute(
                """SELECT relative_path,size,mtime_ns,ctime_ns,digest
                   FROM file_cache WHERE root_path=?""",
                (root_key,),
            ).fetchall()
        else:
            with self._connect() as conn:
                cached_rows = conn.execute(
                    """SELECT relative_path,size,mtime_ns,ctime_ns,digest
                       FROM file_cache WHERE root_path=?""",
                    (root_key,),
                ).fetchall()
        cache = {
            str(row["relative_path"]): (
                int(row["size"]),
                int(row["mtime_ns"]),
                int(row["ctime_ns"]),
                str(row["digest"]),
            )
            for row in cached_rows
        }

        def stop(reason: str) -> None:
            nonlocal complete, stopped_reason
            complete = False
            stopped_reason = stopped_reason or reason

        def walk(directory: Path) -> None:
            nonlocal files_seen, files_reused, bytes_read
            nonlocal oversized_count, budget_count, unstable_count
            if not complete:
                return
            try:
                entries = os.scandir(directory)
            except OSError as exc:
                if directory == root:
                    raise RollbackError(f"无法扫描 workspace: {directory}: {exc}") from exc
                stop("unreadable-directory")
                return
            with entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        stop("timeout")
                        return
                    if entry.name in _EXCLUDED_DIRS:
                        continue
                    path = Path(entry.path)
                    rel = path.relative_to(root).as_posix()
                    if storage_relative and (
                        rel == storage_relative or rel.startswith(storage_relative + "/")
                    ):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        mode = stat.S_IMODE(info.st_mode)
                        if entry.is_symlink() or self._is_reparse_point(info):
                            files_seen += 1
                            if files_seen > self.max_files:
                                stop("file-limit")
                                return
                            manifest[rel] = {
                                "type": "symlink",
                                "target": os.readlink(path),
                                "mode": mode,
                                "directory": entry.is_dir(follow_symlinks=True),
                            }
                        elif entry.is_dir(follow_symlinks=False):
                            manifest[rel] = {"type": "directory", "mode": mode}
                            walk(path)
                            if not complete:
                                return
                        elif entry.is_file(follow_symlinks=False):
                            seen_files.add(rel)
                            files_seen += 1
                            if files_seen > self.max_files:
                                stop("file-limit")
                                return
                            fingerprint = self._file_fingerprint(info)
                            cached = cache.get(rel)
                            if (
                                cached is not None
                                and cached[:3] == fingerprint
                                and (
                                    not store_blobs
                                    or self._object_has_size(cached[3], info.st_size)
                                )
                            ):
                                digest = cached[3]
                                files_reused += 1
                                manifest[rel] = {
                                    "type": "file",
                                    "hash": digest,
                                    "size": info.st_size,
                                    "mode": mode,
                                }
                            elif info.st_size > self.max_file_bytes:
                                ignored.append(rel)
                                oversized_count += 1
                            else:
                                pending_files.append(
                                    (rel, path, info, mode, fingerprint)
                                )
                    except OSError:
                        ignored.append(rel)
                        unstable_count += 1
                        continue

        walk(root)
        # Capture smaller uncached files first. This maximizes the number of
        # restorable paths under the byte budget instead of letting a few
        # archives consume the whole allowance.
        if stopped_reason != "timeout":
            ordered_pending = sorted(
                pending_files,
                key=lambda item: (item[2].st_size, item[0]),
            )
            capture_candidates = []
            reserved_bytes = 0
            for item in ordered_pending:
                if reserved_bytes + item[2].st_size > self.max_snapshot_bytes:
                    ignored.append(item[0])
                    budget_count += 1
                else:
                    capture_candidates.append(item)
                    reserved_bytes += item[2].st_size

            def capture_file(item):
                rel, path, info, mode, fingerprint = item
                digest = (
                    self._store_blob(path, expected=info, deadline=deadline)
                    if store_blobs else self._hash_file(
                        path, expected=info, deadline=deadline
                    )
                )
                return rel, info, mode, fingerprint, digest

            batch_size = max(self.scan_workers * 8, 8)
            with ThreadPoolExecutor(
                max_workers=self.scan_workers,
                thread_name_prefix="rollback-scan",
            ) as executor:
                for batch_start in range(0, len(capture_candidates), batch_size):
                    if time.monotonic() >= deadline:
                        ignored.extend(
                            item[0] for item in capture_candidates[batch_start:]
                        )
                        stop("timeout")
                        break
                    batch = capture_candidates[
                        batch_start:batch_start + batch_size
                    ]
                    bytes_read += sum(item[2].st_size for item in batch)
                    futures = {
                        executor.submit(capture_file, item): item
                        for item in batch
                    }
                    batch_timed_out = False
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            rel, info, mode, fingerprint, digest = future.result()
                        except TimeoutError:
                            ignored.append(item[0])
                            batch_timed_out = True
                            continue
                        except OSError:
                            ignored.append(item[0])
                            unstable_count += 1
                            continue
                        cache_updates.append(
                            (
                                root_key,
                                rel,
                                fingerprint[0],
                                fingerprint[1],
                                fingerprint[2],
                                digest,
                                now_iso(),
                            )
                        )
                        manifest[rel] = {
                            "type": "file",
                            "hash": digest,
                            "size": info.st_size,
                            "mode": mode,
                        }
                    if batch_timed_out:
                        next_start = batch_start + len(batch)
                        ignored.extend(
                            item[0] for item in capture_candidates[next_start:]
                        )
                        stop("timeout")
                        break
        elif pending_files:
            ignored.extend(item[0] for item in pending_files)

        stale_cache_paths = (
            set(cache) - seen_files if complete else set()
        )
        if cache_updates:
            if cache_conn is not None:
                cache_conn.executemany(
                    """INSERT OR REPLACE INTO file_cache
                       (root_path,relative_path,size,mtime_ns,ctime_ns,digest,updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    cache_updates,
                )
            else:
                with self._connect() as conn:
                    conn.executemany(
                        """INSERT OR REPLACE INTO file_cache
                           (root_path,relative_path,size,mtime_ns,ctime_ns,digest,updated_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        cache_updates,
                    )
        if stale_cache_paths:
            target_conn = cache_conn
            if target_conn is not None:
                target_conn.executemany(
                    "DELETE FROM file_cache WHERE root_path=? AND relative_path=?",
                    ((root_key, path) for path in stale_cache_paths),
                )
            else:
                with self._connect() as conn:
                    conn.executemany(
                        "DELETE FROM file_cache WHERE root_path=? AND relative_path=?",
                        ((root_key, path) for path in stale_cache_paths),
                    )
        if oversized_count:
            warnings.append(
                f"{oversized_count} 个文件超过单文件快照上限，未纳入回退"
            )
        if budget_count:
            warnings.append(
                f"{budget_count} 个文件超出本次新增快照数据预算，未纳入回退"
            )
        if unstable_count:
            warnings.append(
                f"{unstable_count} 个文件在扫描期间变化或无法读取，未纳入回退"
            )
        if stopped_reason == "timeout":
            warnings.append("Workspace 扫描达到时间上限，已安全停止")
        elif stopped_reason == "file-limit":
            warnings.append("Workspace 文件数量达到快照上限，已安全停止")
        elif stopped_reason == "unreadable-directory":
            warnings.append("Workspace 包含无法读取的目录，快照未完整覆盖")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return WorkspaceScan(
            entries=manifest,
            ignored_paths=tuple(sorted(set(ignored))),
            complete=complete,
            warnings=tuple(warnings),
            stats={
                "filesSeen": files_seen,
                "filesTracked": sum(
                    entry.get("type") == "file" for entry in manifest.values()
                ),
                "filesReused": files_reused,
                "bytesRead": bytes_read,
                "ignoredCount": len(set(ignored)),
                "elapsedMs": elapsed_ms,
            },
        )

    # -- query ------------------------------------------------------------

    def list_checkpoints(self, session_id: str) -> list[dict]:
        if self.preference(session_id) is not True:
            return []
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
            current_scan = self._scan_workspace_report(workspace, store_blobs=False)
            wanted_scan = self._decode_manifest(row["manifest_json"])
            current = current_scan.entries
            wanted = wanted_scan.entries
            restore_files = tuple(sorted(
                path for path, entry in wanted.items()
                if entry["type"] in ("file", "symlink") and current.get(path) != entry
            ))
            restore_dirs = tuple(sorted(
                path for path, entry in wanted.items()
                if entry["type"] == "directory" and current.get(path) != entry
            ))
            ignored_paths = (
                set(current_scan.ignored_paths) | set(wanted_scan.ignored_paths)
            )
            delete_paths = (
                tuple(sorted(
                    (
                        path
                        for path in set(current) - set(wanted)
                        if not self._path_is_ignored(path, ignored_paths)
                    ),
                    reverse=True,
                ))
                if current_scan.complete and wanted_scan.complete
                else ()
            )
            snapshot = self._decode_session_snapshot(row["session_json"])
            live = self.session_store.get(session_id)
            current_messages = len(live.messages)
            old_messages = len(snapshot.get("messages", []))
            partial = (
                self._restore_is_partial(live, snapshot, row)
                or current_scan.partial
                or wanted_scan.partial
            )
            warnings = tuple(dict.fromkeys(
                (*wanted_scan.warnings, *current_scan.warnings)
            ))
            return RollbackPreview(
                checkpoint_id=row["checkpoint_id"],
                message_preview=row["message_preview"],
                restore_files=restore_files,
                delete_paths=delete_paths,
                restore_directories=restore_dirs,
                messages_to_remove=max(0, current_messages - old_messages),
                partial=partial,
                warnings=warnings,
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
                safety_row = conn.execute(
                    "SELECT manifest_json FROM checkpoints WHERE checkpoint_id=?",
                    (safety_id,),
                ).fetchone()
                if safety_row is None:
                    raise RollbackError("无法读取回退前安全点。")
                safety_scan = self._decode_manifest(safety_row["manifest_json"])
                conn.execute(
                    """INSERT INTO operations
                       (operation_id,session_id,target_checkpoint_id,safety_checkpoint_id,
                        status,created_at) VALUES (?,?,?,?,?,?)""",
                    (operation_id, session_id, row["checkpoint_id"], safety_id, "PREPARED", now_iso()),
                )

            wanted = self._decode_manifest(row["manifest_json"])
            partial = partial or safety_scan.partial
            result_warnings = list(
                dict.fromkeys((*wanted.warnings, *safety_scan.warnings))
            )
            if safety_scan.partial:
                result_warnings.append(
                    "回退前安全点未完整覆盖；已跳过无法保证安全撤销的路径"
                )
            try:
                # Reuse the safety checkpoint scan instead of walking the
                # workspace a second time.  Besides reducing latency, the
                # partial-scan guard in _apply_manifest then guarantees that
                # every changed path can be restored by compensation/undo.
                restored, deleted = self._apply_manifest(
                    Path(binding["root_path"]),
                    wanted,
                    current_scan=safety_scan,
                )
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
                        self._apply_manifest(
                            Path(binding["root_path"]),
                            self._decode_manifest(safety[0]),
                        )
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
                "warnings": list(dict.fromkeys(result_warnings)),
            }

    def undo(self, session_id: str) -> dict:
        return self.rollback(session_id, "undo")

    def _restore_session(self, live: Session, snapshot: dict) -> None:
        restored = Session.from_dict(snapshot)
        next_revision = live.revision + 1
        runtime_preferences = {
            key: live.metadata[key]
            for key in (AUTO_MODE_METADATA_KEY, SANDBOX_MODE_METADATA_KEY)
            if key in live.metadata
        }
        live.title = restored.title
        live.messages = restored.messages
        live.summary = restored.summary
        live.skill_usage = restored.skill_usage
        live.created_at = restored.created_at
        live.updated_at = now_iso()
        live.last_consolidated = restored.last_consolidated
        live.metadata = restored.metadata
        for key in (AUTO_MODE_METADATA_KEY, SANDBOX_MODE_METADATA_KEY):
            live.metadata.pop(key, None)
        live.metadata.update(runtime_preferences)
        # External-agent sessions are append-only.  After restoring an older
        # SJTUClaw conversation, start fresh branches so removed turns cannot
        # leak back into subsequent prompts. Undo rotates again for the same
        # reason.
        live.metadata["pi_session_generation"] = uuid.uuid4().hex
        live.metadata["claude_session_generation"] = uuid.uuid4().hex
        live.metadata.pop("pi_session_owner", None)
        live.metadata.pop("pi_initialized_generation", None)
        live.metadata.pop("claude_session_owner", None)
        live.metadata.pop("claude_initialized_generation", None)
        live.metadata.pop("claude_session_cwd", None)
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

    def _apply_manifest(
        self,
        root: Path,
        wanted: WorkspaceScan | dict | str,
        *,
        current_scan: WorkspaceScan | None = None,
    ) -> tuple[int, int]:
        root = root.resolve()
        wanted_scan = (
            wanted if isinstance(wanted, WorkspaceScan) else self._decode_manifest(wanted)
        )
        current_scan = current_scan or self._scan_workspace_report(
            root, store_blobs=False
        )
        current = current_scan.entries
        wanted_entries = wanted_scan.entries
        deleted = 0
        restored = 0
        ignored_paths = (
            set(current_scan.ignored_paths) | set(wanted_scan.ignored_paths)
        )

        # Never infer deletions from a truncated scan. Restoring known paths is
        # safe, but deleting an unobserved path is not.
        extras = (
            sorted(
                (
                    path
                    for path in set(current) - set(wanted_entries)
                    if not self._path_is_ignored(path, ignored_paths)
                ),
                key=lambda value: (value.count("/"), value),
                reverse=True,
            )
            if current_scan.complete and wanted_scan.complete
            else []
        )
        for relative in extras:
            target = self._safe_target(root, relative)
            self._remove_path(target)
            deleted += 1

        blocked_restores: set[str] = set()
        if current_scan.partial:
            for relative, desired in wanted_entries.items():
                observed = current.get(relative)
                if observed == desired:
                    continue
                # A partial safety snapshot can only compensate paths whose
                # current state was captured.  Missing/ignored paths are
                # ambiguous, while replacing a partially traversed directory
                # could destroy descendants that were never observed.
                if (
                    observed is None
                    or self._path_is_ignored(relative, ignored_paths)
                    or self._path_contains_ignored(relative, ignored_paths)
                    or (
                        observed.get("type") == "directory"
                        and desired.get("type") != "directory"
                    )
                ):
                    blocked_restores.add(relative)

        def restore_is_blocked(relative: str) -> bool:
            return self._path_is_ignored(relative, blocked_restores)

        # Directories first, then files/symlinks.
        for relative, entry in sorted(
            wanted_entries.items(),
            key=lambda item: (item[0].count("/"), item[0]),
        ):
            if entry["type"] != "directory" or restore_is_blocked(relative):
                continue
            target = self._safe_target(root, relative)
            self._assert_real_parents(root, target)
            if self._path_is_link_like(target) or (target.exists() and not target.is_dir()):
                self._remove_path(target)
            target.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(target, entry["mode"])

        for relative, entry in sorted(wanted_entries.items()):
            if current.get(relative) == entry or restore_is_blocked(relative):
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
                try:
                    self._copy_blob_verified(source, tmp, entry["hash"])
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
                if os.name != "nt":
                    os.chmod(target, entry["mode"])
            restored += 1
        return restored, deleted
