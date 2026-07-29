from __future__ import annotations


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


def test_download_registry_expires_old_entries(monkeypatch, tmp_path):
    from claw.tools import download

    download.configure_download_registry(None)
    clock = iter((10.0, 10.0, 12.0))
    monkeypatch.setattr(download, "_DOWNLOAD_TTL_S", 1)
    monkeypatch.setattr(download.time, "time", lambda: next(clock))
    with download._downloads_lock:
        download._downloads.clear()

    path = tmp_path / "result.txt"
    path.write_text("result", encoding="utf-8")
    download_id = download.register_download(path)

    assert download.get_download(download_id) == path
    assert download.get_download(download_id) is None


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
