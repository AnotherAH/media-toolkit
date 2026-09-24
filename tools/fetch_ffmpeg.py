"""Fetch the pinned static ffmpeg build into bin/.

    python tools/fetch_ffmpeg.py              into bin/, if not already there
    python tools/fetch_ffmpeg.py --dest DIR   somewhere else (scratch builds)
    python tools/fetch_ffmpeg.py --force      download again even if present
    python tools/fetch_ffmpeg.py --strict     the pinned build or an error, never
                                              an ffmpeg found on PATH (release builds)

The build is the one pinned in app/assets.py (FFMPEG_PIN): a release-branch
build from yt-dlp's FFmpeg-Builds, which carries yt-dlp's own patches. The pin
is read from that file's source rather than by importing the app, so this
script has no side effects on a data folder and runs before any dependency is
installed.

Besides ffmpeg and ffprobe it keeps the archive's LICENSE.txt as
FFMPEG-LICENSE.txt and writes FFMPEG-VERSION.txt with the exact build, its
SHA-256 and where its source lives. The installer ships all four, which is
what the GPL asks of anyone passing ffmpeg on.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
WANTED = ("ffmpeg", "ffprobe")
LICENSE_NAME = "FFMPEG-LICENSE.txt"
VERSION_NAME = "FFMPEG-VERSION.txt"


def pin(name: str = "FFMPEG_PIN") -> dict:
    """A pin literal from app/assets.py (FFMPEG_PIN or FFMPEG_MAC_PIN)."""
    tree = ast.parse((ROOT / "app" / "assets.py").read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"app/assets.py has no {name}")


def fetch_mac(dest: Path, force: bool) -> int:
    """macOS on Apple silicon: two pinned zips from FFMPEG_MAC_PIN, plus the
    GPL text kept in tools/licenses (the zips carry only the binaries)."""
    p = pin("FFMPEG_MAC_PIN")
    if not force and already_have(dest, p["version"]):
        print(f"ffmpeg: {p['version']} already in {dest}")
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    lines = [f"FFmpeg {p['version']} ({p['license']})"]
    with tempfile.TemporaryDirectory(prefix="mt-ffmpeg-") as tmp:
        for exe, (name, sha, size) in p["assets"].items():
            archive = Path(tmp) / name
            print(f"ffmpeg: downloading {exe} ({size / 1048576:.0f} MB)...")
            download(p["base_url"] + name, archive, sha, size)
            with zipfile.ZipFile(archive) as zf:
                member = next(m for m in zf.namelist() if Path(m).name == exe)
                with zf.open(member) as src, (dest / exe).open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            os.chmod(dest / exe, 0o755)
            lines += [f"Build: {name}", f"Download: {p['base_url']}{name}", f"SHA-256: {sha}"]
    lines += [f"FFmpeg source: {p['source']}", f"Build scripts: {p['builds_repo']}"]
    (dest / VERSION_NAME).write_text("\n".join(lines) + "\n", "utf-8")
    shutil.copy2(ROOT / "tools" / "licenses" / "ffmpeg-gpl-3.0.txt", dest / LICENSE_NAME)
    print(f"ffmpeg: installed {p['version']} into {dest}")
    return 0


def platform_key() -> str | None:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows":
        return {"amd64": "win64", "x86_64": "win64", "arm64": "winarm64"}.get(machine)
    if system == "Linux":
        return {"x86_64": "linux64", "amd64": "linux64", "aarch64": "linuxarm64",
                "arm64": "linuxarm64"}.get(machine)
    return None


def exe_names() -> set[str]:
    suffix = ".exe" if os.name == "nt" else ""
    return {f"{n}{suffix}" for n in WANTED}


def installed_version(dest: Path) -> str:
    """The pinned version recorded next to the binaries, or '' if unknown."""
    try:
        first = (dest / VERSION_NAME).read_text("utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""
    parts = first.split()
    return parts[1] if len(parts) > 1 else ""


def already_have(dest: Path, version: str) -> bool:
    """The pinned build with its GPL text is already in dest."""
    return (all((dest / n).exists() for n in exe_names()) and (dest / LICENSE_NAME).exists()
            and installed_version(dest) == version)


def on_path() -> bool:
    return all(shutil.which(n) for n in WANTED)


def download(url: str, target: Path, sha256: str, size: int) -> None:
    """Stream to disk (the archive is 150 MB) and check the pinned hash."""
    h = hashlib.sha256()
    done = 0
    req = urllib.request.Request(url, headers={"User-Agent": "MediaToolkit-setup"})
    with urllib.request.urlopen(req, timeout=120) as resp, target.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or size or 0)
        step = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total and done * 10 // total > step:
                step = done * 10 // total
                print(f"  {done / 1048576:.0f} of {total / 1048576:.0f} MB", flush=True)
    if h.hexdigest() != sha256:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch: got {h.hexdigest()}, expected {sha256}")


def extract(archive: Path, dest: Path) -> int:
    names = exe_names()
    found = 0
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if base in names:
                    target, found = dest / base, found + 1
                elif base == "LICENSE.txt" and member.count("/") == 1:
                    target = dest / LICENSE_NAME
                else:
                    continue
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    else:
        with tarfile.open(archive, mode="r:xz") as tf:
            for member in tf.getmembers():
                base = Path(member.name).name
                if not member.isfile():
                    continue
                if base in names:
                    target, found = dest / base, found + 1
                elif base == "LICENSE.txt" and member.name.count("/") == 1:
                    target = dest / LICENSE_NAME
                else:
                    continue
                src = tf.extractfile(member)
                if src:
                    with target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    if base in names:
                        os.chmod(target, 0o755)
    return found


def version_text(p: dict, key: str) -> str:
    """Same text as app.assets.ffmpeg_version_text (tests keep them equal)."""
    name, sha, _ = p["assets"][key]
    return (f"FFmpeg {p['version']} ({p['license']})\n"
            f"Build: {name}\n"
            f"Download: {p['base_url']}{name}\n"
            f"SHA-256: {sha}\n"
            f"FFmpeg source: https://github.com/FFmpeg/FFmpeg/tree/{p['ffmpeg_commit']}\n"
            f"Source archive: https://github.com/FFmpeg/FFmpeg/archive/{p['ffmpeg_commit']}.tar.gz\n"
            f"  (FFmpeg {p['ffmpeg_release']}, https://ffmpeg.org/releases/ffmpeg-{p['ffmpeg_release']}.tar.xz,\n"
            f"  plus the fixes on its release branch up to that commit)\n"
            f"Build scripts: {p['builds_repo']}/tree/{p['builds_commit']}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=BIN, help="folder to put the binaries in")
    ap.add_argument("--force", action="store_true", help="download even if present")
    ap.add_argument("--strict", action="store_true",
                    help="fail instead of falling back to an ffmpeg on PATH (for release builds, "
                         "whose licence notes describe the pinned build only)")
    args = ap.parse_args(argv)
    dest: Path = args.dest.resolve()
    if platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
        return fetch_mac(dest, args.force)
    p = pin()

    key = platform_key()
    if not args.force and already_have(dest, p["version"]):
        if key in p["assets"]:
            # Same binaries; refresh the notes in case the pin's source
            # directions were corrected since they were written.
            (dest / VERSION_NAME).write_text(version_text(p, key), "utf-8")
        print(f"ffmpeg: {p['version']} already in {dest}")
        return 0

    if key is None:
        if on_path() and not args.strict:
            print("ffmpeg: no pinned build for this system; using the one on PATH")
            return 0
        print(f"ffmpeg: no pinned build for {platform.system()} {platform.machine()}.")
        print("  macOS:  brew install ffmpeg")
        print("  Linux:  sudo apt install ffmpeg")
        return 1

    name, sha, size = p["assets"][key]
    url = p["base_url"] + name
    old = installed_version(dest)
    if old and old != p["version"]:
        print(f"ffmpeg: replacing {old} with the pinned {p['version']}")
    print(f"ffmpeg: downloading {name} ({size / 1048576:.0f} MB, one time)...")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mt-ffmpeg-") as tmp:
        archive = Path(tmp) / name
        try:
            download(url, archive, sha, size)
        except Exception as exc:
            print(f"ffmpeg: download failed ({exc}).")
            if on_path() and not args.strict:
                print("ffmpeg: found on PATH, using that instead")
                return 0
            return 1
        print("ffmpeg: checksum OK, extracting...")
        staging = Path(tmp) / "out"
        staging.mkdir()
        found = extract(archive, staging)
        if found < len(WANTED):
            print("ffmpeg: archive did not contain the expected binaries")
            return 1
        (staging / VERSION_NAME).write_text(version_text(p, key), "utf-8")
        for f in staging.iterdir():
            shutil.copy2(f, dest / f.name)
    print(f"ffmpeg: installed {p['version']} into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
