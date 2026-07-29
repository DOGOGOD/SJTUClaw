from __future__ import annotations

import json
import time


def test_download_registry_is_bounded(monkeypatch, tmp_path):
    from claw.tools import download

    download.configure_download_registry(None)
    with download._downloads_lock:
        download._downloads.clear()
    monkeypatch.setattr(download, "_MAX_DOWNLOADS", 3)

    paths = []
    download_ids = []
    for index in range(5):
        path = tmp_path / f"file-{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        paths.append(path)
        download_ids.append(download.register_download(path))

    assert download.get_download(download_ids[0]) is None
    assert download.get_download(download_ids[-1]) == paths[-1]
    assert len(download.list_downloads()) == 3


def test_download_registry_keeps_old_entries_while_file_exists(
    monkeypatch, tmp_path
):
    from claw.tools import download

    registry = tmp_path / "downloads" / "registry.json"
    path = tmp_path / "result.txt"
    path.write_text("result", encoding="utf-8")
    try:
        download.configure_download_registry(registry)
        monkeypatch.setattr(download.time, "time", lambda: 10.0)
        download_id = download.register_download(path)

        # Simulate reopening a saved chat long after the former one-hour TTL.
        monkeypatch.setattr(download.time, "time", lambda: 86_410.0)
        download.configure_download_registry(None)
        download.configure_download_registry(registry)

        assert download.get_download(download_id) == path.resolve()
        assert download.get_download(download_id) == path.resolve()
    finally:
        download.configure_download_registry(None)


def test_download_registry_survives_reload(tmp_path):
    from claw.tools import download

    registry = tmp_path / "downloads" / "registry.json"
    file_path = tmp_path / "report.md"
    file_path.write_text("# report", encoding="utf-8")
    try:
        download.configure_download_registry(registry)
        download_id = download.register_download(file_path)

        download.configure_download_registry(None)
        assert download.get_download(download_id) is None

        download.configure_download_registry(registry)
        assert download.get_download(download_id) == file_path.resolve()
    finally:
        download.configure_download_registry(None)


def test_download_registry_refreshes_entry_created_by_another_process(tmp_path):
    from claw.tools import download

    registry = tmp_path / "downloads" / "registry.json"
    file_path = tmp_path / "shared.png"
    file_path.write_bytes(b"shared-image")
    download_id = "dl_0123456789ab"
    try:
        download.configure_download_registry(registry)
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "downloadId": download_id,
                            "path": str(file_path.resolve()),
                            "createdAt": time.time(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        assert download.get_download(download_id) == file_path.resolve()
    finally:
        download.configure_download_registry(None)


def test_register_download_merges_entries_created_by_another_process(tmp_path):
    from claw.tools import download

    registry = tmp_path / "downloads" / "registry.json"
    external_path = tmp_path / "external.txt"
    external_path.write_text("external", encoding="utf-8")
    local_path = tmp_path / "local.txt"
    local_path.write_text("local", encoding="utf-8")
    external_id = "dl_abcdef012345"
    try:
        download.configure_download_registry(registry)
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "downloadId": external_id,
                            "path": str(external_path.resolve()),
                            "createdAt": time.time(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        local_id = download.register_download(local_path)
        download.configure_download_registry(None)
        download.configure_download_registry(registry)

        assert download.get_download(external_id) == external_path.resolve()
        assert download.get_download(local_id) == local_path.resolve()
    finally:
        download.configure_download_registry(None)


def test_capacity_eviction_removes_only_managed_sandbox_export(
    monkeypatch, tmp_path
):
    from claw.tools import download

    export_root = tmp_path / "sandbox" / "exports"
    managed = export_root / "session" / "request" / "result.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("managed", encoding="utf-8")
    ordinary = tmp_path / "workspace.txt"
    ordinary.write_text("workspace", encoding="utf-8")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("replacement", encoding="utf-8")
    monkeypatch.setattr(download, "DATA_DIR", tmp_path)
    monkeypatch.setattr(download, "_MAX_DOWNLOADS", 1)
    download.configure_download_registry(None)

    ordinary_id = download.register_download(ordinary)
    managed_id = download.register_download(managed)
    assert download.get_download(ordinary_id) is None
    assert ordinary.exists()

    replacement_id = download.register_download(replacement)
    assert download.get_download(managed_id) is None
    assert download.get_download(replacement_id) == replacement

    assert not managed.exists()
    assert ordinary.exists()
