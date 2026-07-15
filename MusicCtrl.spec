# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH)
CONDA_BIN = Path(sys.base_prefix) / "Library" / "bin"
CONDA_RUNTIME_DLLS = tuple(
    CONDA_BIN / name
    for name in ("sqlite3.dll", "liblzma.dll", "libbz2.dll", "libmpdec-4.dll", "ffi.dll")
    if (CONDA_BIN / name).is_file()
)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[(str(path), ".") for path in CONDA_RUNTIME_DLLS],
    datas=[
        (str(ROOT / "assets"), "assets"),
        (str(ROOT / "styles"), "styles"),
    ],
    hiddenimports=[
        *collect_submodules("mutagen"),
        "pythoncom",
        "pywintypes",
        "win32com.client",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MusicCtrl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "app_icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MusicCtrl",
)
