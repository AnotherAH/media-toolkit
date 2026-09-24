"""Build the Windows release: frozen app, installer, portable zip, checksums.

    .venv\\Scripts\\python.exe tools\\build.py                  everything, into dist\\
    .venv\\Scripts\\python.exe tools\\build.py --no-installer --no-zip
    .venv\\Scripts\\python.exe tools\\build.py --out %TEMP%\\mt-build   scratch build

One script so a local build and the CI release build cannot drift apart. The
version comes from app/__init__.py and nowhere else. Needs the packages in
requirements.txt and requirements-build.txt, and Inno Setup 6 for the
installer (winget install JRSoftware.InnoSetup).

Steps: fetch the pinned ffmpeg (tools/fetch_ffmpeg.py), run PyInstaller on
MediaToolkit.spec (which also stages bin/, LICENSE.txt and
THIRD-PARTY-NOTICES.txt beside the exe), check the result
(tools/verify_build.py), compile installer/MediaToolkit.iss, zip a portable
copy, and write SHA256SUMS.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

PORTABLE_NOTE = (
    "This copy of Media Toolkit is portable. It keeps its settings, speech models,\r\n"
    "logs and GPU support in the \"data\" folder next to MediaToolkit.exe instead of\r\n"
    "%LOCALAPPDATA%\\Media Toolkit, so it can live on a USB stick and leaves nothing\r\n"
    "behind on the computer. Downloads go to the folder you choose in the app.\r\n"
    "\r\n"
    "If this folder cannot be written to, the normal location is used instead.\r\n"
    "Delete this file to always use the normal location.\r\n")


def version() -> str:
    text = (ROOT / "app" / "__init__.py").read_text("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit("app/__init__.py has no __version__")
    return m.group(1)


def step(title: str) -> None:
    print(f"\n==> {title}", flush=True)


def run(cmd: list[str], env: dict | None = None) -> None:
    print("   ", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, env=env)


def iscc() -> str:
    """ISCC.exe: $ISCC, PATH, or the newest Inno Setup 6+ in the usual places
    (winget installs per user, Chocolatey into Program Files (x86))."""
    found = [os.environ.get("ISCC", ""), shutil.which("iscc") or ""]
    for base in ("%LOCALAPPDATA%\\Programs", "%ProgramFiles(x86)%", "%ProgramFiles%"):
        root = Path(os.path.expandvars(base))
        if root.is_dir():
            dirs = [d for d in root.glob("Inno Setup *") if d.name.split()[-1].isdigit()
                    and int(d.name.split()[-1]) >= 6]
            found += [str(d / "ISCC.exe") for d in sorted(dirs, key=lambda d: -int(d.name.split()[-1]))]
    for cand in found:
        if cand and Path(cand).exists():
            return cand
    sys.exit("Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) "
             "or set ISCC to the path of ISCC.exe.")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def python_age_days() -> int:
    """Days since this Python was built, or 0 when that is unknown."""
    try:
        built = datetime.strptime(" ".join(platform.python_build()[1].split()[:3]), "%b %d %Y")
    except (ValueError, IndexError):
        return 0
    return max((datetime.now() - built).days, 0)


def write_checksums(files: list[Path], dest: Path) -> list[str]:
    """SHA256SUMS.txt in the format `sha256sum -c` reads. LF line endings on
    purpose: with CRLF, sha256sum takes the CR as part of each file name and
    reports every file as missing."""
    lines = [f"{sha256(p)}  {p.name}" for p in files]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return lines


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def mb(n: int) -> str:
    return f"{n / 1048576:.0f} MB"


def make_zip(app: Path, out: Path) -> None:
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(app.rglob("*")):
            if f.is_file():
                zf.write(f, Path("MediaToolkit") / f.relative_to(app))
        # The marker that keeps settings, models and downloads in a data
        # folder beside the exe instead of %LOCALAPPDATA% (app/config.py).
        zf.writestr("MediaToolkit/portable.txt", PORTABLE_NOTE)
        # The installer shows these terms as its licence page; a zip has no
        # such page, so they travel as a file (the Intel and NVIDIA runtime
        # licences ask for end-user terms that protect their rights).
        zf.write(ROOT / "installer" / "terms.txt", "MediaToolkit/TERMS.txt")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist",
                    help="output folder (default: dist). Work files go to <out>/../build "
                         "for the default, <out>/build otherwise")
    ap.add_argument("--ffmpeg-dir", type=Path, default=ROOT / "bin",
                    help="where the pinned ffmpeg is fetched to and taken from (default: bin)")
    ap.add_argument("--no-installer", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--print-version", action="store_true",
                    help="print the version from app/__init__.py and exit")
    args = ap.parse_args(argv)

    ver = version()
    if args.print_version:
        print(ver)
        return 0
    out: Path = args.out.resolve()
    work = ROOT / "build" if out == (ROOT / "dist").resolve() else out / "build"
    app = out / "MediaToolkit"
    ffdir: Path = args.ffmpeg_dir.resolve()
    print(f"Media Toolkit {ver} -> {out}")
    age = python_age_days()
    if age > 180:
        # The build copies this Python's OpenSSL, expat and SQLite into the
        # app, so their security fixes only reach users through a newer Python.
        print(f"    NOTE: this Python ({platform.python_version()}) was built {age} days ago. "
              "A release should use the newest patch release of its Python version "
              "(the release workflow does).", flush=True)

    step("ffmpeg")
    run([sys.executable, "tools/fetch_ffmpeg.py", "--strict", "--dest", str(ffdir)])

    step("PyInstaller")
    env = dict(os.environ, MT_FFMPEG_DIR=str(ffdir))
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(out), "--workpath", str(work), "MediaToolkit.spec"], env=env)

    step("Checking the build")
    import verify_build
    problems = verify_build.check(app, ver)
    for p in problems:
        print("    PROBLEM:", p)
    if problems:
        return 1
    internal, binsize = folder_size(app / "_internal"), folder_size(app / "bin")
    print(f"    _internal {mb(internal)}, bin {mb(binsize)}, total {mb(folder_size(app))}")

    outputs: list[Path] = []
    if not args.no_installer:
        step("Installer")
        run([iscc(), f"/DAppVersion={ver}", f"/DSourceDir={app}", f"/O{out}", "/Q",
             str(ROOT / "installer" / "MediaToolkit.iss")])
        setup = out / f"MediaToolkit-Setup-{ver}.exe"
        if not setup.exists():
            sys.exit(f"Inno Setup did not produce {setup.name}")
        outputs.append(setup)
        print(f"    {setup.name}: {mb(setup.stat().st_size)}")

    if not args.no_zip:
        step("Portable zip")
        zip_path = out / f"MediaToolkit-{ver}-portable.zip"
        make_zip(app, zip_path)
        outputs.append(zip_path)
        print(f"    {zip_path.name}: {mb(zip_path.stat().st_size)}")

    if outputs:
        step("Checksums")
        for line in write_checksums(outputs, out / "SHA256SUMS.txt"):
            print("   ", line)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
