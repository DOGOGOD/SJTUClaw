# -*- mode: python ; coding: utf-8 -*-

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(SPECPATH).parents[1]
APP_ICON = ROOT / "packaging" / "windows" / "assets" / "SJTUClaw.ico"


def add_tree(name: str):
    source = ROOT / name
    return [(str(source), name)] if source.exists() else []


datas = []
binaries = []
hiddenimports = []
datas += add_tree("web")
datas += add_tree("prompts")
datas += add_tree("skills")
datas += add_tree("claw/pet/assets")
for pi_extension in ("permission_gate.ts", "sjtuclaw_provider.ts", "sjtuclaw_tools.ts"):
    source = ROOT / "claw" / "pi" / pi_extension
    if source.exists():
        datas.append((str(source), "claw/pi"))
if (ROOT / ".env.example").exists():
    datas.append((str(ROOT / ".env.example"), "."))

# The wheel bundles its native extension, msb and libkrunfw.
sandbox_datas, sandbox_binaries, sandbox_hiddenimports = collect_all(
    "microsandbox",
    on_error="raise",
)
native_sandbox_spec = importlib.util.find_spec("microsandbox._microsandbox")
if (
    native_sandbox_spec is None
    or native_sandbox_spec.origin is None
    or not Path(native_sandbox_spec.origin).is_file()
):
    raise RuntimeError("microsandbox native extension is not installed")
required_sandbox_files = {"msb.exe", "libkrunfw.dll"}
collected_sandbox_files = {
    Path(source).name
    for source, _destination in sandbox_datas + sandbox_binaries
}
missing_sandbox_files = required_sandbox_files - collected_sandbox_files
if missing_sandbox_files:
    raise RuntimeError(
        "microsandbox runtime collection is incomplete; missing: "
        + ", ".join(sorted(missing_sandbox_files))
    )
datas += sandbox_datas
binaries += sandbox_binaries
hiddenimports += sandbox_hiddenimports

hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("webview")
hiddenimports += [
    "claw.desktop",
    "claw.gateway.server",
    "claw.pet",
    "claw.pet.__main__",
    "tkinter",
]

# pywebview supports several optional GUI backends. SJTUClaw uses the native
# Windows backend, so collecting Qt bindings is unnecessary and breaks builds
# when the build environment happens to contain more than one Qt package.
excludes = [
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
]

a = Analysis(
    [str(ROOT / "claw" / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SJTUClaw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=str(APP_ICON),
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SJTUClaw",
)
