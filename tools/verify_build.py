"""Check a frozen build before it is packaged.

    python tools/verify_build.py [dist\\MediaToolkit]

The licence position of the installer rests on things a dependency upgrade
or a new PyInstaller hook could silently undo: no GPL code in the process
(PyAV, mutagen), no NVIDIA library file in the download, the notices (with
the Intel and NVIDIA terms) and GPL source directions beside the exe, and an
ffmpeg that is exactly the pinned build those directions describe.
tools/build.py runs this after PyInstaller and refuses to package a build
that fails it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Top-level modules that must not be in the PYZ archive.
FORBIDDEN_MODULES = {"av", "mutagen"}
# Folders/files that must not be in _internal.
FORBIDDEN_PATHS = re.compile(r"(^|/)(av|av\.libs|mutagen|nvidia)(/|$)", re.I)
FORBIDDEN_DLLS = re.compile(r"(^|/)(cudnn|cublas|cudart|nvrtc|libx264|libx265)[^/]*\.dll$", re.I)
REQUIRED = ("MediaToolkit.exe", "LICENSE.txt", "THIRD-PARTY-NOTICES.txt",
            "bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/FFMPEG-LICENSE.txt",
            "bin/FFMPEG-VERSION.txt")


def pyz_modules(exe: Path) -> set[str]:
    from PyInstaller.archive.readers import CArchiveReader
    pkg = CArchiveReader(str(exe))
    names: set[str] = set()
    for name in pkg.toc:
        if name.endswith(".pyz"):
            names |= set(pkg.open_embedded_archive(name).toc)
    return names


def exe_version(exe: Path) -> str:
    from PyInstaller.utils.win32 import versioninfo
    info = versioninfo.read_version_info_from_executable(str(exe))
    if info is None:
        return ""
    for kid in info.kids:
        for table in getattr(kid, "kids", []):
            for s in getattr(table, "kids", []):
                if getattr(s, "name", "") == "ProductVersion":
                    return str(s.val)
    return ""


def ffmpeg_banner(exe: Path) -> str:
    """First line of `ffmpeg -version`, or '' when it does not run."""
    import subprocess
    try:
        out = subprocess.run([str(exe), "-hide_banner", "-version"], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.splitlines() or [""])[0].strip()


def check(app: Path, version: str) -> list[str]:
    problems: list[str] = []
    for rel in REQUIRED:
        if not (app / rel).is_file():
            problems.append(f"missing {rel}")
    internal = app / "_internal"
    for f in internal.rglob("*"):
        rel = f.relative_to(internal).as_posix()
        if FORBIDDEN_PATHS.search(rel) or FORBIDDEN_DLLS.search(rel):
            problems.append(f"must not ship {rel}")
    exe = app / "MediaToolkit.exe"
    if exe.is_file():
        tops = {m.split(".")[0] for m in pyz_modules(exe)}
        for mod in sorted(FORBIDDEN_MODULES & tops):
            problems.append(f"module {mod} is inside the executable")
        if sys.platform == "win32":
            got = exe_version(exe)
            if got != version:
                problems.append(f"exe version resource says {got!r}, app/__init__.py says {version!r}")
    # The GPL source directions name one exact build; a bin/ left over from
    # an older pin or a hand-copied nightly must never ship under them.
    import fetch_ffmpeg
    pinned = fetch_ffmpeg.pin()["version"]
    got = fetch_ffmpeg.installed_version(app / "bin")
    if (app / "bin" / "FFMPEG-VERSION.txt").is_file() and got != pinned:
        problems.append(f"bin/ holds ffmpeg {got or '?'}, but the pin is {pinned}")
    ffmpeg = app / "bin" / "ffmpeg.exe"
    if sys.platform == "win32" and ffmpeg.is_file():
        banner = ffmpeg_banner(ffmpeg)
        if f"version {pinned}" not in banner:
            problems.append(f"bin/ffmpeg.exe is not the pinned {pinned}: {banner[:80] or 'it did not run'}")
    notices = app / "THIRD-PARTY-NOTICES.txt"
    if notices.is_file():
        text = notices.read_text("utf-8", errors="replace")
        for needle in ("Intel Simplified Software License",
                       "Intel End User License Agreement for Developer Tools",
                       "NVIDIA CUDA runtime (cudart_static",
                       "GNU GENERAL PUBLIC LICENSE", "Corresponding source",
                       "PYTHON SOFTWARE FOUNDATION LICENSE"):
            if needle not in text:
                problems.append(f"THIRD-PARTY-NOTICES.txt lacks {needle!r}")
        for gpl_pkg in ("mutagen ", "\nav "):
            if gpl_pkg in text.split("Summary", 1)[-1].split("DOWNLOADED LATER", 1)[0]:
                problems.append(f"THIRD-PARTY-NOTICES.txt lists {gpl_pkg.strip()}, which must not ship")
    return problems


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "tools"))
    from build import version as _version
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "MediaToolkit"
    found = check(target, _version())
    for p in found:
        print("PROBLEM:", p)
    print("OK" if not found else f"{len(found)} problem(s)")
    sys.exit(1 if found else 0)
