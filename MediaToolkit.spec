# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build.

Deliberately excludes the NVIDIA CUDA wheels (~2 GB). The app fetches the one
library it actually needs (cuBLAS, ~740 MB) on demand via app/assets.py, and
only on machines that have an NVIDIA GPU. Everything else is bundled so the
installed app never needs Python or pip.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "README.md"), "."),
]
# faster-whisper ships the Silero VAD ONNX model as package data.
datas += collect_data_files("faster_whisper")

binaries = []
for pkg in ("ctranslate2", "onnxruntime", "av", "tokenizers"):
    binaries += collect_dynamic_libs(pkg)
# No tkinter here on purpose: a venv carries no tcl/tk for PyInstaller to find,
# and the folder chooser uses PowerShell on Windows instead (app/folderpick.py).

hiddenimports = [
    "app", "app.main", "app.config", "app.media", "app.transcribe", "app.subs",
    "app.jobs", "app.hardware", "app.cookies", "app.ffmpegtools", "app.recode",
    "app.assets", "app.folderpick", "app.shell", "app.live", "app.models",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    # tkinter intentionally absent -- see the binaries note above.
    "websockets", "websockets.sync", "websockets.sync.client",
    "encodings.idna",
]
# yt-dlp resolves extractors lazily by name; without the whole tree the frozen
# build supports almost no sites.
hiddenimports += collect_submodules("yt_dlp")
hiddenimports += collect_submodules("faster_whisper")
hiddenimports += collect_submodules("ctranslate2")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "nvidia",              # fetched on demand instead (see app/assets.py)
        "matplotlib", "scipy", "pandas", "PIL", "IPython", "notebook",
        "pytest", "setuptools", "pip", "wheel", "PyInstaller",
        "torch", "transformers",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaToolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # standalone app window; startup problems go to app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico") if (ROOT / "assets" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MediaToolkit",
)
