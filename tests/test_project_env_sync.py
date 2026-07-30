from pathlib import Path
import sys

import pytest

from claw.sandbox import project_env_sync


def _site(root: Path) -> Path:
    tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return root / "lib" / tag / "site-packages"


def test_project_environment_restore_and_save_round_trip(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    runtime = tmp_path / "runtime"
    project_site = _site(project)
    runtime_site = _site(runtime)
    project_site.mkdir(parents=True)
    runtime_site.mkdir(parents=True)
    (project / "bin").mkdir(parents=True)
    (runtime / "bin").mkdir(parents=True)
    (project_site / "old_package.py").write_text(
        "VALUE = 'persisted'\n",
        encoding="utf-8",
    )
    (project / "bin" / "project-cli").write_text(
        "#!/bin/sh\n",
        encoding="utf-8",
    )
    (runtime / "bin" / "python").write_text("", encoding="utf-8")
    (runtime / "bin" / "pip").write_text("", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(
        project_env_sync,
        "_BASELINE_FILE",
        str(baseline),
    )

    project_env_sync.restore(project, runtime)

    assert (runtime_site / "old_package.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'persisted'\n"
    assert (runtime / "bin" / "project-cli").exists()

    (runtime_site / "old_package.py").unlink()
    (runtime_site / "new_package.py").write_text(
        "VALUE = 'new'\n",
        encoding="utf-8",
    )
    (runtime / "bin" / "new-cli").write_text(
        "#!/bin/sh\n",
        encoding="utf-8",
    )
    project_env_sync.save(project, runtime)

    assert not (project_site / "old_package.py").exists()
    assert (project_site / "new_package.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'new'\n"
    assert sorted(path.name for path in (project / "bin").iterdir()) == [
        "new-cli",
        "project-cli",
    ]
    assert not (project / "bin" / "python").exists()
    assert not (project / "bin" / "pip").exists()

    reads: list[Path] = []
    original_read = project_env_sync._read_bytes

    def tracked_read(path: Path) -> bytes:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(project_env_sync, "_read_bytes", tracked_read)
    project_env_sync.save(project, runtime)

    assert reads == []


def test_write_bytes_cleans_up_temporary_file_when_write_stalls(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "package.py"
    monkeypatch.setattr(project_env_sync.os, "write", lambda _fd, _view: 0)

    with pytest.raises(OSError, match="failed to make progress"):
        project_env_sync._write_bytes(destination, b"content", 0o644, 0)

    assert not destination.exists()
    assert list(tmp_path.glob(".package.py.sjtuclaw-sync-*")) == []
