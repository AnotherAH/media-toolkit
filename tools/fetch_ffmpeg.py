"""Fetch a static ffmpeg build into bin/ if one is not already there.

Uses the builds yt-dlp publishes, which carry its own patches.
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
WANTED = ("ffmpeg", "ffprobe")

RELEASES = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
ASSETS = {
    ("Windows", "AMD64"): "ffmpeg-master-latest-win64-gpl.zip",
    ("Windows", "ARM64"): "ffmpeg-master-latest-winarm64-gpl.zip",
    ("Linux", "x86_64"): "ffmpeg-master-latest-linux64-gpl.tar.xz",
    ("Linux", "aarch64"): "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
}


def already_have() -> bool:
    suffix = ".exe" if os.name == "nt" else ""
    return all((BIN / f"{n}{suffix}").exists() for n in WANTED)


def on_path() -> bool:
    return all(shutil.which(n) for n in WANTED)


def main() -> int:
    if already_have():
        print("ffmpeg: already in bin/")
        return 0

    key = (platform.system(), platform.machine())
    asset = ASSETS.get(key)
    if not asset:
        if on_path():
            print("ffmpeg: found on PATH, using that")
            return 0
        print(f"ffmpeg: no prebuilt download for {key}.")
        print("  macOS:  brew install ffmpeg")
        print("  Linux:  sudo apt install ffmpeg")
        return 1

    BIN.mkdir(parents=True, exist_ok=True)
    url = RELEASES + asset
    print(f"ffmpeg: downloading {asset} (about 40-90 MB, one time)...")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            blob = resp.read()
    except Exception as exc:
        print(f"ffmpeg: download failed ({exc}).")
        if on_path():
            print("ffmpeg: found on PATH, using that instead")
            return 0
        return 1

    print("ffmpeg: extracting...")
    found = 0
    suffix = ".exe" if os.name == "nt" else ""
    names = {f"{n}{suffix}" for n in WANTED}

    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if base in names:
                    with zf.open(member) as src, (BIN / base).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found += 1
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz") as tf:
            for member in tf.getmembers():
                base = Path(member.name).name
                if base in names and member.isfile():
                    src = tf.extractfile(member)
                    if src:
                        with (BIN / base).open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.chmod(BIN / base, 0o755)
                        found += 1

    if found:
        print(f"ffmpeg: installed {found} binaries into bin/")
        return 0
    print("ffmpeg: archive did not contain the expected binaries")
    return 1


if __name__ == "__main__":
    sys.exit(main())
