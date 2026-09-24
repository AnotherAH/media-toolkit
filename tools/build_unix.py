"""Build the Linux or macOS release (tools/build.py does Windows).

    python tools/build_unix.py            build, package and smoke-test
    python tools/build_unix.py --no-smoke

Linux:  dist/MediaToolkit-<version>-linux-x86_64.tar.gz and, when appimagetool
        is available ($APPIMAGETOOL or PATH), dist/MediaToolkit-<version>-x86_64.AppImage
macOS:  dist/MediaToolkit-<version>-macos-arm64.dmg holding "Media Toolkit.app"

The builds are not code-signed with a developer identity. On macOS the app is
ad-hoc signed (Apple silicon refuses to run unsigned code at all), so people
open it the first time with right-click > Open.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MACOS = sys.platform == "darwin"

DESKTOP = """[Desktop Entry]
Type=Application
Name=Media Toolkit
Comment=Download videos, record live streams and make transcripts
Exec=MediaToolkit
Icon=media-toolkit
Categories=AudioVideo;Network;
Terminal=false
"""

APPRUN = """#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/media-toolkit/MediaToolkit" "$@"
"""


def version() -> str:
    text = (ROOT / "app" / "__init__.py").read_text("utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def run(cmd: list[str], **kw) -> None:
    print("   ", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def smoke(exe: Path, ver: str) -> None:
    """Start the frozen app headless and ask it for its version."""
    home = Path(tempfile.mkdtemp(prefix="mt-smoke-"))
    env = dict(os.environ, MEDIA_TOOLKIT_HOME=str(home))
    port = 8997
    proc = subprocess.Popen([str(exe), "--server", "--port", str(port)], env=env)
    try:
        for _ in range(90):
            time.sleep(1)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/about", timeout=3) as r:
                    body = r.read().decode()
                break
            except OSError:
                if proc.poll() is not None:
                    raise SystemExit(f"smoke test: the app exited with {proc.returncode}")
        else:
            raise SystemExit("smoke test: the app never answered")
        if f'"version":"{ver}"' not in body.replace(" ", ""):
            raise SystemExit(f"smoke test: unexpected /api/about: {body[:300]}")
        print(f"    smoke test OK: {body[:120]}")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)


def package_linux(ver: str) -> list[Path]:
    app = DIST / "MediaToolkit"
    out = []
    tgz = DIST / f"MediaToolkit-{ver}-linux-{arch()}.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(app, arcname=f"MediaToolkit-{ver}")
    out.append(tgz)

    tool = os.environ.get("APPIMAGETOOL") or shutil.which("appimagetool")
    if tool:
        appdir = Path(tempfile.mkdtemp(prefix="mt-appdir-")) / "MediaToolkit.AppDir"
        shutil.copytree(app, appdir / "usr/lib/media-toolkit", symlinks=True)
        (appdir / "media-toolkit.desktop").write_text(DESKTOP, "utf-8")
        shutil.copy2(ROOT / "assets/icon.png", appdir / "media-toolkit.png")
        (appdir / "AppRun").write_text(APPRUN, "utf-8")
        os.chmod(appdir / "AppRun", 0o755)
        image = DIST / f"MediaToolkit-{ver}-{arch()}.AppImage"
        run([tool, "--appimage-extract-and-run", str(appdir), str(image)],
            env=dict(os.environ, ARCH=arch()))
        out.append(image)
    else:
        print("    appimagetool not found: skipping the AppImage")
    return out


def package_macos(ver: str) -> list[Path]:
    app = DIST / "Media Toolkit.app"
    # bin/ and the licence files were added after PyInstaller signed the
    # bundle; sign it again (ad hoc) so macOS accepts every binary in it.
    run(["codesign", "--force", "--deep", "--sign", "-", str(app)])
    stage = Path(tempfile.mkdtemp(prefix="mt-dmg-"))
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE.txt")
    dmg = DIST / f"MediaToolkit-{ver}-macos-{arch()}.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", f"Media Toolkit {ver}", "-srcfolder", str(stage),
         "-ov", "-format", "UDZO", str(dmg)])
    return [dmg]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-smoke", action="store_true")
    args = ap.parse_args()
    ver = version()
    print(f"Media Toolkit {ver} for {platform.system()} {arch()}")
    run([sys.executable, "tools/fetch_ffmpeg.py", "--strict"])
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "MediaToolkit.spec"])
    exe = (DIST / "Media Toolkit.app/Contents/MacOS/MediaToolkit") if MACOS \
        else (DIST / "MediaToolkit/MediaToolkit")
    if not args.no_smoke:
        smoke(exe, ver)
    files = package_macos(ver) if MACOS else package_linux(ver)
    for f in files:
        print(f"    {f.name}: {f.stat().st_size / 1048576:.0f} MB")


if __name__ == "__main__":
    main()
