"""Concurrency and Windows sharing-violation coverage for SessionStore."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claw.session.store import SessionStore, SessionStoreError


def test_concurrent_auto_ids_create_distinct_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions")

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(store.create_session) for _ in range(40)]
        sessions = [future.result() for future in futures]

    assert len({session.session_id for session in sessions}) == 40
    assert len(store.list_summaries()) == 40


def test_session_listing_is_safe_during_concurrent_creation(tmp_path):
    store = SessionStore(tmp_path / "sessions")

    with ThreadPoolExecutor(max_workers=12) as executor:
        create_futures = [executor.submit(store.create_session) for _ in range(30)]
        list_futures = [
            executor.submit(
                lambda: [store.list_summaries() for _ in range(20)]
            )
            for _ in range(4)
        ]
        for future in [*create_futures, *list_futures]:
            future.result()

    assert len(store.list_summaries()) == 30


def test_store_instances_allocate_unique_auto_ids(tmp_path):
    sessions_dir = tmp_path / "sessions"
    stores = [SessionStore(sessions_dir), SessionStore(sessions_dir)]

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(stores[index % len(stores)].create_session)
            for index in range(30)
        ]
        sessions = [future.result() for future in futures]

    assert len({session.session_id for session in sessions}) == 30
    assert len(SessionStore(sessions_dir).list_summaries()) == 30


def test_fork_does_not_overwrite_existing_target(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    source = store.create_session(session_id="source")
    source.append_message("user", "source message")
    store.save(source)
    target = store.create_session(session_id="target")
    target.append_message("user", "keep me")
    store.save(target)

    with pytest.raises(SessionStoreError, match="session id 已存在"):
        store.fork_session_before_user_index("source", "target", 1)

    reloaded = SessionStore(tmp_path / "sessions").get("target")
    assert [message.content for message in reloaded.messages] == ["keep me"]


def test_save_rejects_invalid_session_id(tmp_path):
    from claw.session.models import Session

    store = SessionStore(tmp_path / "sessions")

    with pytest.raises(SessionStoreError, match="非法字符"):
        store.save(Session(session_id="../bad", title="bad"))


def test_save_retries_transient_replace_denial(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="retry-save", title="Retry")
    session.append_message("user", "persist me")

    real_replace = os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError(13, "destination is temporarily in use")
        return real_replace(source, destination)

    monkeypatch.setattr("claw.session.store.os.replace", flaky_replace)

    store.save(session)

    assert attempts == 3
    reloaded = SessionStore(tmp_path / "sessions").get("retry-save")
    assert [message.content for message in reloaded.messages] == ["persist me"]


def test_failed_create_does_not_publish_phantom_session(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    attempts = 0

    def always_denied(source, destination):
        nonlocal attempts
        attempts += 1
        raise PermissionError(13, "destination remains in use")

    monkeypatch.setattr("claw.session.store.os.replace", always_denied)
    monkeypatch.setattr("claw.session.store.time.sleep", lambda _delay: None)

    with pytest.raises(SessionStoreError, match="保存 session blocked-create 失败"):
        store.create_session(session_id="blocked-create")

    assert attempts == 6
    assert not store.exists("blocked-create")
    assert not list((tmp_path / "sessions").glob("*.tmp"))


def test_same_store_serializes_writes_for_one_session(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="same-store")
    session.append_message("user", "stable snapshot")

    real_write = SessionStore._write_jsonl
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def observed_write(target_session, path, *, fsync=False):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.01)
            real_write(target_session, path, fsync=fsync)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(SessionStore, "_write_jsonl", staticmethod(observed_write))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(store.save, session) for _ in range(24)]
        for future in futures:
            future.result()

    assert max_active == 1
    assert SessionStore(tmp_path / "sessions").get("same-store").messages[0].content == "stable snapshot"


def test_store_instances_share_a_file_lock(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    first_store = SessionStore(sessions_dir)
    first_session = first_store.create_session(session_id="shared-store")
    first_session.append_message("user", "first snapshot")
    first_store.save(first_session)

    second_store = SessionStore(sessions_dir)
    second_session = second_store.get("shared-store")

    real_write = SessionStore._write_jsonl
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def observed_write(target_session, path, *, fsync=False):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.03)
            real_write(target_session, path, fsync=fsync)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(SessionStore, "_write_jsonl", staticmethod(observed_write))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_store.save, first_session)
        second = executor.submit(second_store.save, second_session)
        first.result()
        second.result()

    assert max_active == 1
    reloaded = SessionStore(sessions_dir).get("shared-store")
    assert len(reloaded.messages) == 1


def test_loader_skips_valid_json_lines_with_wrong_shape(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    path = sessions_dir / "mixed.jsonl"
    path.write_text(
        "\n".join(
            [
                "[]",
                '"unexpected string"',
                (
                    '{"_type":"metadata","key":"mixed","metadata":[],'
                    '"last_consolidated":"bad","revision":{}}'
                ),
                '{"role":"user","content":"kept"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = SessionStore(sessions_dir)
    session = store.get("mixed")

    assert [message.content for message in session.messages] == ["kept"]
    assert session.metadata == {}
    assert session.last_consolidated == 0
    assert session.revision == 0
    assert store.load_warnings


def test_delete_failure_keeps_session_available(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="undeletable")
    session_path = store._key_path("undeletable")
    real_unlink = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == session_path:
            raise PermissionError(13, "locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)

    with pytest.raises(SessionStoreError, match="删除 session undeletable 失败"):
        store.delete("undeletable")

    assert store.get("undeletable") is session
    assert session_path.exists()


def test_loader_quarantines_filename_metadata_key_mismatch(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    encoded_path = SessionStore(sessions_dir)._key_path("expected-id")
    encoded_path.write_text(
        '{"_type":"metadata","key":"different-id","metadata":{}}\n',
        encoding="utf-8",
    )

    store = SessionStore(sessions_dir)

    assert not store.exists("expected-id")
    assert not store.exists("different-id")
    assert store.load_warnings
    assert list(sessions_dir.glob("*.corrupted-*"))
