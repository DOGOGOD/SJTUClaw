from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import claw.sandbox.runtime as runtime
from claw.sandbox.runtime import (
    SandboxManager,
    SandboxError,
    _IMAGE_SUBSYSTEM_WINDOWS_CUI,
    _IMAGE_SUBSYSTEM_WINDOWS_GUI,
    _configure_frozen_windows_microsandbox,
    _pe_subsystem_offset,
    _prepare_windows_gui_msb,
)


def _pe_image(*, subsystem: int) -> bytes:
    image = bytearray(512)
    image[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset:pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<H", image, pe_offset + 20, 0xF0)
    optional_offset = pe_offset + 24
    struct.pack_into("<H", image, optional_offset, 0x20B)
    struct.pack_into("<H", image, optional_offset + 68, subsystem)
    return bytes(image)


def _subsystem(image: bytes) -> int:
    return struct.unpack_from("<H", image, _pe_subsystem_offset(image))[0]


def test_prepare_windows_gui_msb_patches_cached_copy_only(tmp_path: Path) -> None:
    source = tmp_path / "bundle" / "bin" / "msb.exe"
    source.parent.mkdir(parents=True)
    source_lib = tmp_path / "bundle" / "lib" / "libkrunfw.dll"
    source_lib.parent.mkdir()
    source_lib.write_bytes(b"libkrunfw-fixture")
    original = _pe_image(subsystem=_IMAGE_SUBSYSTEM_WINDOWS_CUI)
    source.write_bytes(original)

    target = _prepare_windows_gui_msb(source, cache_dir=tmp_path / "cache")

    assert target.parent.parent.parent == tmp_path / "cache"
    assert target != source
    assert source.read_bytes() == original
    assert _subsystem(target.read_bytes()) == _IMAGE_SUBSYSTEM_WINDOWS_GUI
    assert (
        target.parent.parent / "lib" / "libkrunfw.dll"
    ).read_bytes() == source_lib.read_bytes()
    assert _prepare_windows_gui_msb(
        source,
        cache_dir=tmp_path / "cache",
    ) == target


def test_prepare_windows_gui_msb_reuses_gui_binary(tmp_path: Path) -> None:
    source = tmp_path / "msb.exe"
    source.write_bytes(_pe_image(subsystem=_IMAGE_SUBSYSTEM_WINDOWS_GUI))

    assert _prepare_windows_gui_msb(source, cache_dir=tmp_path / "cache") == source
    assert not (tmp_path / "cache").exists()


def test_prepare_windows_gui_msb_rejects_invalid_binary(tmp_path: Path) -> None:
    source = tmp_path / "msb.exe"
    source.write_bytes(b"not-a-pe")

    with pytest.raises(SandboxError, match="PE"):
        _prepare_windows_gui_msb(source, cache_dir=tmp_path / "cache")


def test_frozen_windows_availability_uses_gui_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("microsandbox")
    bundled = tmp_path / "bundle" / "bin" / "msb.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled")
    gui = tmp_path / "cache" / "bin" / "msb.exe"
    gui.parent.mkdir(parents=True)
    gui.write_bytes(b"gui")
    calls: list[list[str]] = []

    monkeypatch.delenv("MSB_PATH", raising=False)
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(
        "microsandbox._runtime.msb_path",
        lambda: bundled,
    )
    monkeypatch.setattr(
        runtime,
        "_prepare_windows_gui_msb",
        lambda source: gui if source == bundled else source,
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert SandboxManager.sdk_available()
    assert calls == [[str(gui), "doctor"]]


def test_frozen_windows_backend_uses_environment_override_after_sdk_import(
    monkeypatch,
    tmp_path: Path,
) -> None:
    imported_sdk = pytest.importorskip("microsandbox")
    bundled = tmp_path / "bundle" / "bin" / "msb.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled")
    gui = tmp_path / "cache" / "bin" / "msb.exe"
    gui.parent.mkdir(parents=True)
    gui.write_bytes(b"gui")
    gui_lib = gui.parent.parent / "lib" / "libkrunfw.dll"
    gui_lib.parent.mkdir()
    gui_lib.write_bytes(b"lib")

    monkeypatch.delenv("MSB_PATH", raising=False)
    monkeypatch.delenv("MSB_LIBKRUNFW_PATH", raising=False)
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(
        "microsandbox._runtime.msb_path",
        lambda: bundled,
    )
    monkeypatch.setattr(
        runtime,
        "_prepare_windows_gui_msb",
        lambda source: gui if source == bundled else source,
    )

    # The concrete module is intentionally already imported here, matching
    # MicrosandboxBackend.__init__ and the frozen application's real order.
    _configure_frozen_windows_microsandbox(imported_sdk)

    assert runtime.os.environ["MSB_PATH"] == str(gui)
    assert runtime.os.environ["MSB_LIBKRUNFW_PATH"] == str(gui_lib)


def test_native_resolver_honors_msb_environment_after_sdk_import(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("microsandbox")
    native = pytest.importorskip("microsandbox._microsandbox")
    expected = tmp_path / "late-override" / "msb.exe"

    monkeypatch.setenv("MSB_PATH", str(expected))

    assert native.resolved_msb_path() == str(expected)
