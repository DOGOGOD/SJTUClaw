"""Concurrency and Windows sharing-violation coverage for SessionStore."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from claw.session.store import SessionStore, SessionStoreError


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
