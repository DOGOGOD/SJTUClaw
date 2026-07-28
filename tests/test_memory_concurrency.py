from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def test_concurrent_memory_adds_keep_unique_ids_and_files(tmp_path):
    from claw.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "memory")

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(store.add, f"concurrent memory {index}")
            for index in range(40)
        ]
        entries = [future.result() for future in futures]

    assert len(store.list()) == 40
    assert len({entry.memory_id for entry in entries}) == 40
    assert len(list((tmp_path / "memory").glob("*/*.md"))) == 40


def test_corrupt_numeric_frontmatter_is_skipped_on_load(tmp_path):
    from claw.memory.store import MemoryStore

    target = tmp_path / "memory" / "general" / "broken.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\n"
        'id: "mem_broken"\n'
        'category: "general"\n'
        'importance: "not-a-number"\n'
        'recall_count: "also-invalid"\n'
        "---\n\n"
        "This entry should be skipped.\n",
        encoding="utf-8",
    )

    store = MemoryStore(tmp_path / "memory")

    assert store.list() == []


def test_memory_delete_failure_keeps_runtime_entry(tmp_path, monkeypatch):
    from claw.memory.store import MemoryStore, MemoryStoreError

    store = MemoryStore(tmp_path / "memory")
    entry = store.add("keep this memory")
    target = entry._file_path
    real_unlink = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == target:
            raise PermissionError(13, "locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)

    with pytest.raises(MemoryStoreError, match="删除 memory"):
        store.delete(entry.memory_id)

    assert store.list() == [entry]
    assert target is not None and target.exists()


def test_memory_update_write_failure_rolls_back_runtime_state(
    tmp_path, monkeypatch
):
    from claw.memory.store import MemoryStore, MemoryStoreError

    store = MemoryStore(tmp_path / "memory")
    entry = store.add("original memory")
    old_path = entry._file_path

    def fail_write(_entry):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(store, "_write_md_file", fail_write)

    with pytest.raises(MemoryStoreError, match="更新 memory"):
        store.update(entry.memory_id, "changed memory")

    assert entry.content == "original memory"
    assert entry._file_path == old_path
