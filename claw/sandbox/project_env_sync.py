"""Synchronize a project Python environment across a passthrough mount.

microsandbox 0.6.7 on Windows reports EACCES when a read-only FUSE handle is
flushed.  The bytes were read successfully, so this copier deliberately
ignores that specific close error while moving persisted project packages
between ``/workspace/.venv`` and the microVM-local runtime venv.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import uuid
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024
_BASELINE_FILE = "/opt/sjtuclaw/project-bin-baseline.json"


def _close_ignoring_readonly_flush(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EPERM}:
            raise


def _read_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, _CHUNK_SIZE):
            chunks.append(chunk)
    finally:
        _close_ignoring_readonly_flush(fd)
    return b"".join(chunks)


def _write_bytes(
    path: Path,
    payload: bytes,
    mode: int,
    mtime_ns: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.sjtuclaw-sync-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("failed to make progress while writing")
                view = view[written:]
        finally:
            _close_ignoring_readonly_flush(fd)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
    except NotImplementedError:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def _relative_files(root: Path) -> dict[Path, Path]:
    if not root.is_dir():
        return {}
    files: dict[Path, Path] = {}
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if name != "__pycache__"]
        parent = Path(directory)
        for filename in filenames:
            if filename.endswith((".pyc", ".pyo")):
                continue
            source = parent / filename
            files[source.relative_to(root)] = source
    return files


def _remove_stale(destination: Path, expected: set[Path]) -> None:
    if not destination.is_dir():
        return
    for relative, path in sorted(
        _relative_files(destination).items(),
        reverse=True,
    ):
        if relative not in expected:
            path.unlink(missing_ok=True)
    for directory, names, _files in os.walk(destination, topdown=False):
        for name in names:
            child = Path(directory) / name
            try:
                child.rmdir()
            except OSError:
                pass


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    exclude_names: set[str] | None = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    source_files = _relative_files(source)
    if exclude_names:
        source_files = {
            relative: path
            for relative, path in source_files.items()
            if relative.parts[0] not in exclude_names
        }
    _remove_stale(destination, set(source_files))
    for relative, source_path in source_files.items():
        destination_path = destination / relative
        source_stat = source_path.lstat()
        if stat.S_ISLNK(source_stat.st_mode):
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.unlink(missing_ok=True)
            destination_path.symlink_to(os.readlink(source_path))
            continue
        try:
            destination_stat = destination_path.lstat()
        except OSError:
            destination_stat = None
        if (
            destination_stat is not None
            and stat.S_ISREG(destination_stat.st_mode)
            and destination_stat.st_size == source_stat.st_size
            and destination_stat.st_mtime_ns == source_stat.st_mtime_ns
            and stat.S_IMODE(destination_stat.st_mode)
            == stat.S_IMODE(source_stat.st_mode)
        ):
            continue
        _write_bytes(
            destination_path,
            _read_bytes(source_path),
            stat.S_IMODE(source_stat.st_mode) or 0o644,
            source_stat.st_mtime_ns,
        )


def _paths(
    project_root: Path,
    runtime_root: Path,
) -> tuple[Path, Path, Path, Path]:
    python_tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    project_site = project_root / "lib" / python_tag / "site-packages"
    runtime_site = runtime_root / "lib" / python_tag / "site-packages"
    return project_site, runtime_site, project_root / "bin", runtime_root / "bin"


def restore(project_root: Path, runtime_root: Path) -> None:
    """Restore persisted packages and scripts into a fresh runtime venv."""
    project_site, runtime_site, project_bin, runtime_bin = _paths(
        project_root,
        runtime_root,
    )
    baseline = sorted(path.name for path in runtime_bin.iterdir())
    Path(_BASELINE_FILE).write_text(
        json.dumps(baseline),
        encoding="utf-8",
    )
    _copy_tree(project_site, runtime_site)
    if project_bin.is_dir():
        for relative, source in _relative_files(project_bin).items():
            source_stat = source.lstat()
            _write_bytes(
                runtime_bin / relative,
                _read_bytes(source),
                stat.S_IMODE(source_stat.st_mode) or 0o755,
                source_stat.st_mtime_ns,
            )


def save(project_root: Path, runtime_root: Path) -> None:
    """Persist the runtime venv's project packages and console scripts."""
    project_site, runtime_site, project_bin, runtime_bin = _paths(
        project_root,
        runtime_root,
    )
    _copy_tree(runtime_site, project_site)
    baseline = set(json.loads(Path(_BASELINE_FILE).read_text(encoding="utf-8")))
    _copy_tree(runtime_bin, project_bin, exclude_names=baseline)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"restore", "save"}:
        print(
            "usage: project_env_sync.py restore|save PROJECT_ROOT RUNTIME_ROOT",
            file=sys.stderr,
        )
        return 2
    operation = restore if sys.argv[1] == "restore" else save
    operation(Path(sys.argv[2]), Path(sys.argv[3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
