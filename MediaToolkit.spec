# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of Media Toolkit (one folder: MediaToolkit.exe + _internal).

    python -m PyInstaller --noconfirm --clean MediaToolkit.spec
    python tools/build.py          the same, plus installer, portable zip, checksums

What is deliberately NOT in the build, and why:

* av (PyAV) and mutagen. Both are GPL in practice (PyAV's FFmpeg links x264
  and x265; mutagen is GPL-2.0-or-later), and this process also loads Intel's
  proprietary MKL/OpenMP through CTranslate2. GPL code and proprietary code
  must not share one process. Audio is decoded by the bundled ffmpeg.exe in a
  separate process instead (app/audio.py), and app.audio.install_av_stub()
  satisfies faster-whisper's unused `import av`.
* NVIDIA's cudnn64_9.dll, which CTranslate2's wheel carries. ctranslate2.dll
  never references cuDNN (no import, no delay-load, no string); the file was
  only loaded because ctranslate2/__init__.py loads every DLL in its folder.
  A frozen build without it was measured transcribing on an RTX 5080 with
  only the GPU pack's cuBLAS (tools/verify_gpu.py repeats that check), so
  the installer carries no NVIDIA library file. (ctranslate2.dll itself has
  NVIDIA's CUDA runtime compiled in, which NVIDIA lists as redistributable;
  THIRD-PARTY-NOTICES.txt and installer/terms.txt pass on its terms.)
* The NVIDIA CUDA wheels (~2 GB). The app fetches the one library Whisper
  needs (cuBLAS) on demand via app/assets.py, only on NVIDIA machines.

Everything else is bundled so the installed app never needs Python or pip.

After COLLECT this spec also stages the files that sit next to the exe:
bin/ffmpeg.exe and bin/ffprobe.exe with their GPL text and exact version,
LICENSE.txt, and THIRD-PARTY-NOTICES.txt written from what the build
actually contains. The ffmpeg folder defaults to <repo>/bin and can be
pointed elsewhere with MT_FFMPEG_DIR.
"""
import os
import re
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files
from PyInstaller.utils.win32 import versioninfo as vi

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT / "tools"))

VERSION = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (ROOT / "app" / "__init__.py").read_text("utf-8")).group(1)

FFMPEG_DIR = Path(os.environ.get("MT_FFMPEG_DIR") or ROOT / "bin")
FFMPEG_FILES = ("ffmpeg.exe", "ffprobe.exe")
FFMPEG_EXTRAS = ("FFMPEG-LICENSE.txt", "FFMPEG-VERSION.txt")
missing = [n for n in FFMPEG_FILES + FFMPEG_EXTRAS if not (FFMPEG_DIR / n).exists()]
if missing:
    raise SystemExit(f"{FFMPEG_DIR} lacks {', '.join(missing)}. "
                     "Run: python tools/fetch_ffmpeg.py")

# Kept out of the process for licence reasons (see the docstring).
GPL_EXCLUDES = ["av", "mutagen"]
NVIDIA_DLL = re.compile(r"(^|[\\/])(cudnn|cublas|cudart|nvrtc)[^\\/]*\.dll$", re.I)


# ------------------------------------------------------------ version resource

def version_file() -> str:
    """Windows version resource for the exe, written from app/__init__.py
    into the build folder (never committed, so it cannot go stale)."""
    nums = [int(x) for x in re.findall(r"\d+", VERSION)[:4]]
    nums += [0] * (4 - len(nums))
    strings = [
        vi.StringStruct("CompanyName", "Media Toolkit"),
        vi.StringStruct("FileDescription", "Media Toolkit"),
        vi.StringStruct("FileVersion", VERSION),
        vi.StringStruct("InternalName", "MediaToolkit"),
        vi.StringStruct("LegalCopyright", "Copyright (c) 2026 AnotherAH. MIT License."),
        vi.StringStruct("OriginalFilename", "MediaToolkit.exe"),
        vi.StringStruct("ProductName", "Media Toolkit"),
        vi.StringStruct("ProductVersion", VERSION),
    ]
    info = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums), mask=0x3F, flags=0x0,
                             OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
              vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])])],
    )
    path = Path(workpath) / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(info), "utf-8")
    return str(path)


# -------------------------------------------------------------------- analysis

datas = [(str(ROOT / "app" / "static"), "app/static")]
# faster-whisper ships the Silero VAD ONNX model as package data.
datas += collect_data_files("faster_whisper")

binaries = []
for pkg in ("ctranslate2", "onnxruntime", "tokenizers"):
    binaries += [b for b in collect_dynamic_libs(pkg) if not NVIDIA_DLL.search(b[0])]
# No tkinter here on purpose: a venv carries no tcl/tk for PyInstaller to find,
# and the folder chooser uses PowerShell on Windows instead (app/folderpick.py).

# Every module of the app, found on disk so a new module can never be left
# out of the frozen build by a stale list.
hiddenimports = sorted(f"app.{p.stem}" for p in (ROOT / "app").glob("*.py") if p.stem != "__init__")
hiddenimports += [
    "app",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "websockets", "websockets.sync", "websockets.sync.client",
    "encodings.idna",
    # app.updater byte-compiles a downloaded yt-dlp and reads versions
    # through the import system; these are stdlib but nothing else pulls them in.
    "compileall", "py_compile", "pkgutil",
]
# yt-dlp resolves extractors lazily by name; without the whole tree the frozen
# build supports almost no sites.
hiddenimports += collect_submodules("yt_dlp")


def stdlib_modules() -> list[str]:
    """The whole standard library, minus developer tools and GUI toolkits.

    A newer yt-dlp downloaded later (app/updater.py) runs on this build's
    Python. PyInstaller only bundles the stdlib modules today's code imports,
    so an update that starts using another one (tomllib, graphlib, ...) would
    fail to import. Shipping all of it costs a few MB and removes that risk.
    """
    import importlib.util
    skip = {"antigravity", "this", "idlelib", "tkinter", "turtle", "turtledemo",
            "ensurepip", "venv", "pydoc", "pydoc_data", "doctest", "unittest", "pdb",
            "lib2to3", "test", "msilib", "zipapp", "tabnanny", "pyclbr", "cProfile",
            "profile", "pstats", "trace", "timeit", "curses"}
    names: list[str] = []
    for name in sorted(sys.stdlib_module_names - skip):
        if name.startswith("_"):
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is None:
            continue                         # POSIX-only (fcntl, pwd, termios...)
        names.append(name)
        if spec.submodule_search_locations:
            # collect_submodules imports what it lists; a __main__ module
            # would run its command (asyncio's REPL, sqlite3's shell...).
            names += collect_submodules(
                name, filter=lambda m: not any(p in skip or p.startswith("test") or p == "__main__"
                                               for p in m.split(".")[1:]))
    return names


hiddenimports += stdlib_modules()
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
    excludes=GPL_EXCLUDES + [
        "nvidia",              # fetched on demand instead (see app/assets.py)
        "matplotlib", "scipy", "pandas", "PIL", "IPython", "notebook",
        "pytest", "setuptools", "pip", "wheel", "PyInstaller",
        "torch", "transformers", "tkinter",
    ],
    noarchive=False,
    optimize=0,
)

# Belt and braces: whatever a hook added, nothing NVIDIA and nothing from the
# excluded GPL packages reaches the output.
def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    top = dest.split("/", 1)[0].split(".", 1)[0]
    if NVIDIA_DLL.search(dest):
        return False
    if top in GPL_EXCLUDES or dest.startswith(("av.libs/", "av/")):
        return False
    return True

a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

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
    version=version_file(),
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

# ------------------------------------------------ files that sit beside the exe

APP_DIR = Path(DISTPATH) / "MediaToolkit"
(APP_DIR / "bin").mkdir(exist_ok=True)
for name in FFMPEG_FILES + FFMPEG_EXTRAS:
    shutil.copy2(FFMPEG_DIR / name, APP_DIR / "bin" / name)
shutil.copy2(ROOT / "LICENSE", APP_DIR / "LICENSE.txt")

import third_party  # noqa: E402  (tools/, put on sys.path above)

tops = third_party.top_levels_from_toc(a.pure, a.binaries, a.datas)
notices = third_party.write(APP_DIR / "THIRD-PARTY-NOTICES.txt", tops, APP_DIR / "bin")
print(f"MediaToolkit {VERSION}: staged bin/, LICENSE.txt, {notices.name} "
      f"({notices.stat().st_size // 1024} KB)")
