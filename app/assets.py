"""On-demand runtime packs, fetched without pip.

The installer stays small, and carries no NVIDIA library file, by leaving two
pieces out and fetching them only when they are actually needed:

* **GPU support** - NVIDIA cuBLAS, taken from NVIDIA's own wheel on PyPI. Only
  for machines with an NVIDIA card. CTranslate2 (Whisper's engine) is built
  without cuDNN and routes all of Whisper through cuBLAS, so cuBLAS is the only
  CUDA library the app needs; the cudnn64_9.dll that CTranslate2's wheel
  carries is left out of the build (MediaToolkit.spec) because nothing uses it.
* **ffmpeg** - normally shipped with the installer; this is the repair path.

Both downloads are pinned to one exact file and checked against its SHA-256
before anything is unpacked, so a compromised mirror or a truncated download
can never put a different binary on the user's machine. They stream to a file
on disk instead of into memory: the cuBLAS wheel alone is 528 MB.

Wheels are plain zip files, so a URL fetch plus a member extract is all it takes.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import shutil
import socket
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from threading import Lock

from . import config

# ------------------------------------------------------------------ GPU pack

# A pinned, tested build. The newest wheel on PyPI is not automatically the
# best: a cuBLAS that expects a newer driver than the user has fails at the
# first transcription instead of at download time. Bump these three together,
# after a GPU transcription on real hardware (see tools/build.py --help).
CUBLAS_WHEEL = {
    "package": "nvidia-cublas-cu12",
    "version": "12.9.2.10",
    "filename": "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
    "url": "https://files.pythonhosted.org/packages/20/e2/fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
    "size": 553162896,
    "sha256": "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
}
# Everything CTranslate2 actually resolves at runtime for Whisper on CUDA.
CUBLAS_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
# Bytes on disk once unpacked (the two DLLs plus NVIDIA's licence text),
# measured from the pinned wheel.
GPU_PACK_DISK_BYTES = 771_251_070

GPU_PACK_DOWNLOAD_MB = round(CUBLAS_WHEEL["size"] / 1048576)
GPU_PACK_DISK_MB = round(GPU_PACK_DISK_BYTES / 1048576)
# What the UI quotes before the download ("a one-time 528 MB download").
GPU_PACK_SIZE_MB = GPU_PACK_DOWNLOAD_MB

NVIDIA_LICENSE = {
    "name": "NVIDIA Software License Agreement (CUDA Toolkit EULA)",
    "url": "https://docs.nvidia.com/cuda/eula/index.html",
    # Shown next to the download button, before anything is fetched.
    "text": ("GPU support is NVIDIA's cuBLAS library, downloaded from NVIDIA's "
             "official package. It is NVIDIA's own software, not part of Media "
             "Toolkit. Downloading it means you accept NVIDIA's license terms."),
}

# -------------------------------------------------------------------- ffmpeg

# The exact build the installer ships and the repair path downloads.
# tools/fetch_ffmpeg.py reads this literal (it does not import the app), so
# there is one pin for both. It is a release-branch build: the FFmpeg 7.1.1
# release plus the fixes FFmpeg backported to its release/7.1 branch, never
# the moving development branch. It comes from a month-end yt-dlp release,
# which the project keeps (its daily releases disappear within weeks), and it
# is the newest release-branch build yt-dlp published: after August 2025 its
# builds follow FFmpeg's development branch only. ffmpeg_commit and
# builds_commit (the FFmpeg-Builds scripts, which pin every bundled library)
# together identify the complete corresponding source the GPL asks for.
FFMPEG_PIN = {
    "version": "n7.1.1-57-g1b48158a23",
    "release": "autobuild-2025-08-31-14-15",
    "base_url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/autobuild-2025-08-31-14-15/",
    "ffmpeg_release": "7.1.1",
    "ffmpeg_commit": "1b48158a23f1f2d4d5bc6a8de60b7fc049061da2",
    "builds_repo": "https://github.com/yt-dlp/FFmpeg-Builds",
    "builds_commit": "72ed75947cffdef640ec21dd7fa7370284673bfa",
    "license": "GPL-3.0-or-later",
    "assets": {
        "win64": ["ffmpeg-n7.1.1-57-g1b48158a23-win64-gpl-7.1.zip",
                  "81a7830116074cff9dd3eddb7d7c9647f8487c8772d1fb2f83ee3af9c82e6dc5", 155218499],
        "winarm64": ["ffmpeg-n7.1.1-57-g1b48158a23-winarm64-gpl-7.1.zip",
                     "55a94d31dd3ec77d102a6063b6ea77f54f3f36417bebf6b3dfa578a4c5a3d845", 108105842],
        "linux64": ["ffmpeg-n7.1.1-57-g1b48158a23-linux64-gpl-7.1.tar.xz",
                    "a61416dd6b6ba23a3a0cf135267edbcc6ef7b84867592d9968b3ec224d70c733", 116774268],
        "linuxarm64": ["ffmpeg-n7.1.1-57-g1b48158a23-linuxarm64-gpl-7.1.tar.xz",
                       "a9fcfceca69aa7e321ab9359b5031d6548f8712c53979233fff2a3abf316f174", 98799300],
    },
}
FFMPEG_EXES = ("ffmpeg.exe", "ffprobe.exe")
FFMPEG_LICENSE_NAME = "FFMPEG-LICENSE.txt"
FFMPEG_VERSION_NAME = "FFMPEG-VERSION.txt"


def ffmpeg_platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    """FFMPEG_PIN asset key for this machine, or None when there is no build."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Windows":
        return {"amd64": "win64", "x86_64": "win64", "arm64": "winarm64"}.get(machine)
    if system == "Linux":
        return {"x86_64": "linux64", "amd64": "linux64", "aarch64": "linuxarm64",
                "arm64": "linuxarm64"}.get(machine)
    return None


def ffmpeg_asset(key: str) -> tuple[str, str, str, int]:
    """(url, file name, sha256, size) of the pinned build for one platform key."""
    name, sha, size = FFMPEG_PIN["assets"][key]
    return FFMPEG_PIN["base_url"] + name, name, sha, size


# ------------------------------------------------------------ progress state

_lock = Lock()
_state: dict = {"busy": False, "task": "", "percent": 0.0, "message": "", "error": "",
                "bytes_done": 0, "bytes_total": 0}


def state() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _claim(task: str, message: str) -> bool:
    """Take the single download slot. Check and set happen under one lock, so
    two clicks cannot start two 528 MB downloads."""
    with _lock:
        if _state["busy"]:
            return False
        _state.update(busy=True, task=task, percent=0.0, message=message, error="",
                      bytes_done=0, bytes_total=0)
        return True


class PackError(Exception):
    """A failure with a message that is already fit to show the user."""


# ------------------------------------------------------------------- helpers

def _downloads_dir() -> Path:
    # Same volume as the destination, so the final move is a rename.
    d = config.RUNTIME_DIR / ".downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trash_dir() -> Path:
    return config.RUNTIME_DIR / ".trash"


def _empty_trash() -> None:
    """Delete what earlier removals could not (DLLs that were loaded then)."""
    trash = _trash_dir()
    if trash.is_dir():
        shutil.rmtree(trash, ignore_errors=True)


def _to_trash(path: Path) -> None:
    """Move a file or folder out of the way. Works even for a DLL this process
    has loaded: Windows refuses to delete a loaded DLL but allows moving it."""
    if not path.exists():
        return
    trash = _trash_dir()
    trash.mkdir(parents=True, exist_ok=True)
    os.replace(path, trash / f"{path.name}-{time.time_ns()}")


def opener() -> urllib.request.OpenerDirector:
    """urllib opener that honours the proxy set in Settings.

    urllib speaks HTTP proxies only. A SOCKS proxy (socks5://) cannot be used
    here, so those downloads go direct; yt-dlp still uses it for sites."""
    handlers: list = []
    proxy = str(config.get().get("proxy") or "").strip()
    if proxy.lower().startswith(("http://", "https://")):
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 1 << 62          # unknown: do not block on it


def _need_space(path: Path, needed: int) -> None:
    free = _free_bytes(path)
    if free < needed:
        drive = Path(path).anchor.rstrip("\\") or str(path)
        raise PackError(f"Not enough free space on {drive}. It needs about "
                        f"{needed / 1073741824:.1f} GB free, and "
                        f"{free / 1073741824:.1f} GB is free.")


def _friendly(exc: BaseException) -> str:
    """Plain-language reason for a failed download."""
    if isinstance(exc, PackError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "The file is no longer on the download server."
        return f"The download server answered with an error ({exc.code})."
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return "Couldn't reach the download server. Check your internet connection."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "The disk is full."
    if isinstance(exc, PermissionError):
        return "Windows would not let the app write the files. Close any program using them and try again."
    if isinstance(exc, zipfile.BadZipFile):
        return "The download was damaged. Try again."
    return str(exc)[:300] or type(exc).__name__


def _mb(n: int) -> str:
    return f"{n / 1048576:.0f}"


def _reporter(label: str, base: float, span: float):
    """on_progress callback that feeds the shared pack state."""
    def report(done: int, total: int) -> None:
        frac = min(done / total, 1.0) if total else 0.0
        _set(percent=round(base + span * frac, 1), bytes_done=done, bytes_total=total,
             message=f"{label} · {_mb(done)} of {_mb(total)} MB" if total
             else f"{label} · {_mb(done)} MB")
    return report


def download(url: str, dest: Path, sha256: str, on_progress=None, size_hint: int = 0,
             attempts: int = 4) -> Path:
    """Stream url to dest and prove it is the pinned file.

    on_progress(done, total) is called a few times a second. A dropped
    connection resumes with an HTTP range request instead of starting the
    whole file again. The hash covers every byte written, and a mismatch
    deletes the file: nothing unverified is ever left for the caller.
    Used by app/updater.py too.
    """
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    hasher = hashlib.sha256()
    done = 0
    total = size_hint
    op = opener()
    for attempt in range(attempts):
        headers = {"User-Agent": "MediaToolkit"}
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            with op.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                if done and resp.status != 206:
                    # Server ignored the range: start over cleanly.
                    hasher, done = hashlib.sha256(), 0
                    part.unlink(missing_ok=True)
                length = int(resp.headers.get("Content-Length") or 0)
                if length and not done:
                    total = length
                with part.open("ab") as out:
                    last = 0.0
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        hasher.update(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if on_progress and now - last > 0.25:
                            last = now
                            on_progress(done, total)
            if total and done < total:
                raise ConnectionError("connection closed early")
            break
        except urllib.error.HTTPError:
            part.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
                part.unlink(missing_ok=True)
                raise
            if attempt == attempts - 1:
                part.unlink(missing_ok=True)
                raise
            time.sleep(2 * (attempt + 1))

    if on_progress:
        on_progress(done, total or done)
    if hasher.hexdigest().lower() != sha256.lower():
        part.unlink(missing_ok=True)
        raise PackError("The download was damaged or is not the expected file. Try again.")
    os.replace(part, dest)
    return dest


def _pypi_file(package: str, version: str, filename: str) -> tuple[str, str]:
    """(url, sha256) for one file of one release, straight from PyPI's JSON."""
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with opener().open(urllib.request.Request(url, headers={"User-Agent": "MediaToolkit"}),
                        timeout=30) as r:
        data = json.loads(r.read())
    for entry in data.get("urls", []):
        if entry.get("filename") == filename:
            return entry["url"], entry["digests"]["sha256"]
    raise PackError(f"{package} {version} is no longer on PyPI.")


# ------------------------------------------------------------------ GPU pack

def gpu_pack_installed() -> bool:
    return all((config.CUDA_DIR / d).exists() for d in CUBLAS_DLLS)


def gpu_pack_info() -> dict:
    """What is installed: the marker written by install_gpu_pack, if any.
    Packs from 1.1 have no marker (they took whatever PyPI had then)."""
    try:
        return json.loads((config.CUDA_DIR / "pack.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def ffmpeg_installed() -> bool:
    return config.ffmpeg_dir() is not None


def status() -> dict:
    # The UI polls this during a pack install, which moves the old pack into
    # the trash at the end: emptying it at that moment could pull the folder
    # out from under that move.
    if not state()["busy"]:
        _empty_trash()
    gpus: list = []
    try:
        from . import hardware
        gpus = hardware.probe_gpus()
    except Exception:
        gpus = []
    installed = gpu_pack_installed()
    return {
        "gpu_pack_installed": installed,
        "gpu_pack_needed": bool(gpus) and not installed,
        "gpu_name": gpus[0]["name"] if gpus else "",
        "gpu_pack_size_mb": GPU_PACK_DOWNLOAD_MB,
        "gpu_pack_disk_mb": GPU_PACK_DISK_MB,
        "gpu_pack_version": gpu_pack_info().get("version", "") if installed else CUBLAS_WHEEL["version"],
        "gpu_pack_license": dict(NVIDIA_LICENSE),
        "ffmpeg_installed": ffmpeg_installed(),
        "ffmpeg_version": FFMPEG_PIN["version"],
        "progress": state(),
    }


def install_gpu_pack() -> dict:
    """Fetch cuBLAS out of NVIDIA's official wheel, pinned and verified."""
    if not _claim("gpu", "Looking up GPU support"):
        return {"ok": False, "error": "Another download is already running."}
    wheel: Path | None = None
    try:
        if os.name != "nt" or ffmpeg_platform_key() != "win64":
            raise PackError("GPU support is available for 64-bit Windows on Intel or AMD processors.")
        _empty_trash()
        _need_space(config.RUNTIME_DIR, CUBLAS_WHEEL["size"] + GPU_PACK_DISK_BYTES + (100 << 20))

        url, sha = CUBLAS_WHEEL["url"], CUBLAS_WHEEL["sha256"]
        try:
            listed_url, listed_sha = _pypi_file(CUBLAS_WHEEL["package"], CUBLAS_WHEEL["version"],
                                                CUBLAS_WHEEL["filename"])
        except PackError:
            raise
        except Exception:
            listed_url, listed_sha = url, sha         # PyPI JSON unreachable: the pin still holds
        if listed_sha.lower() != sha.lower():
            raise PackError("PyPI lists a different file than the tested one, so nothing was downloaded.")

        wheel = download(listed_url, _downloads_dir() / CUBLAS_WHEEL["filename"], sha,
                         _reporter("Downloading GPU support", 0, 90), CUBLAS_WHEEL["size"])

        _set(percent=92.0, message="Unpacking GPU support")
        staging = config.RUNTIME_DIR / "cuda.new"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        found = 0
        with zipfile.ZipFile(wheel) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if base in CUBLAS_DLLS:
                    target = staging / base
                elif member.endswith("/licenses/License.txt") or member.endswith("/License.txt"):
                    # NVIDIA's EULA travels with the libraries it covers.
                    target = staging / "NVIDIA-LICENSE.txt"
                else:
                    continue
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                found += base in CUBLAS_DLLS
        if found < len(CUBLAS_DLLS):
            raise PackError("The download did not contain the expected libraries.")
        (staging / "pack.json").write_text(json.dumps({
            "package": CUBLAS_WHEEL["package"], "version": CUBLAS_WHEEL["version"],
            "sha256": CUBLAS_WHEEL["sha256"], "license_url": NVIDIA_LICENSE["url"],
            "installed": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2), "utf-8")

        _set(percent=98.0, message="Finishing")
        _to_trash(config.CUDA_DIR)                    # an older pack, possibly loaded right now
        os.replace(staging, config.CUDA_DIR)
        _empty_trash()

        config.bootstrap()                            # make them loadable in this process
        from . import hardware
        hardware.forget()                             # re-prove the backend next run
        _set(percent=100.0, message="GPU support is installed. Transcription now runs on your graphics card.")
        return {"ok": True, "path": str(config.CUDA_DIR), "files": found,
                "version": CUBLAS_WHEEL["version"],
                "message": "GPU support is installed. Transcription now runs on your graphics card."}
    except Exception as exc:
        msg = "Couldn't install GPU support. " + _friendly(exc)
        _set(error=msg, message=msg)
        return {"ok": False, "error": msg}
    finally:
        if wheel is not None:
            wheel.unlink(missing_ok=True)
        shutil.rmtree(config.RUNTIME_DIR / "cuda.new", ignore_errors=True)
        _set(busy=False)


def remove_gpu_pack() -> dict:
    """Remove GPU support.

    After a GPU transcription the cuBLAS DLLs are loaded in this process and
    Windows will not delete them. They are moved to a trash folder instead,
    which stops the next start from loading them, and the trash is emptied the
    next time the app looks at the packs.
    """
    if state()["busy"] and state()["task"] == "gpu":
        return {"ok": False, "error": "GPU support is still downloading."}
    try:
        _to_trash(config.CUDA_DIR)
    except OSError as exc:
        return {"ok": False, "error": "Couldn't remove GPU support. " + _friendly(exc)}
    _empty_trash()
    from . import hardware
    hardware.forget()
    leftover = _trash_dir().exists() and any(_trash_dir().iterdir())
    if leftover:
        return {"ok": True, "restart_needed": True,
                "message": "GPU support is removed. Restart Media Toolkit to free the disk space."}
    return {"ok": True, "restart_needed": False, "message": "GPU support is removed."}


# -------------------------------------------------------------------- ffmpeg

def install_ffmpeg() -> dict:
    """Repair: fetch the pinned ffmpeg build into DATA_ROOT/runtime/bin."""
    if os.name != "nt":
        return {"ok": False, "error": "Install ffmpeg with your package manager on this system."}
    key = ffmpeg_platform_key()
    if key not in ("win64", "winarm64"):
        return {"ok": False, "error": "There is no ffmpeg download for this processor."}
    if not _claim("ffmpeg", "Downloading ffmpeg"):
        return {"ok": False, "error": "Another download is already running."}
    archive: Path | None = None
    staging = config.RUNTIME_DIR / "bin.new"
    try:
        url, name, sha, size = ffmpeg_asset(key)
        _empty_trash()
        _need_space(config.RUNTIME_DIR, size + 300 * 1048576)
        archive = download(url, _downloads_dir() / name, sha,
                           _reporter("Downloading ffmpeg", 0, 85), size)

        _set(percent=88.0, message="Unpacking ffmpeg")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        found = extract_ffmpeg_zip(archive, staging)
        if found < len(FFMPEG_EXES):
            raise PackError("The download did not contain ffmpeg.")
        write_ffmpeg_version(staging, key)

        dest = config.RUNTIME_DIR / "bin"
        _to_trash(dest)                               # an ffmpeg in use can be moved, not deleted
        os.replace(staging, dest)
        _empty_trash()
        config.bootstrap()
        _set(percent=100.0, message="ffmpeg is installed.")
        return {"ok": True, "path": str(dest), "files": found, "version": FFMPEG_PIN["version"],
                "message": "ffmpeg is installed."}
    except Exception as exc:
        msg = "Couldn't install ffmpeg. " + _friendly(exc)
        _set(error=msg, message=msg)
        return {"ok": False, "error": msg}
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        _set(busy=False)


def extract_ffmpeg_zip(archive: Path, dest: Path) -> int:
    """Copy ffmpeg.exe, ffprobe.exe and the build's GPL text out of a pinned
    zip. Returns how many of the executables were found. Mirrors extract()
    in tools/fetch_ffmpeg.py, so the repair path and the installer carry the
    same files."""
    found = 0
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            base = Path(member).name
            if base in FFMPEG_EXES:
                target = dest / base
                found += 1
            elif base == "LICENSE.txt" and member.count("/") == 1:
                target = dest / FFMPEG_LICENSE_NAME
            else:
                continue
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    return found


def ffmpeg_version_text(key: str) -> str:
    """FFMPEG-VERSION.txt: the exact build and where its source lives. The
    same text as tools/fetch_ffmpeg.py writes for the installer (a test
    checks they agree)."""
    p = FFMPEG_PIN
    url, name, sha, _ = ffmpeg_asset(key)
    return (f"FFmpeg {p['version']} ({p['license']})\n"
            f"Build: {name}\n"
            f"Download: {url}\n"
            f"SHA-256: {sha}\n"
            f"FFmpeg source: https://github.com/FFmpeg/FFmpeg/tree/{p['ffmpeg_commit']}\n"
            f"Source archive: https://github.com/FFmpeg/FFmpeg/archive/{p['ffmpeg_commit']}.tar.gz\n"
            f"  (FFmpeg {p['ffmpeg_release']}, https://ffmpeg.org/releases/ffmpeg-{p['ffmpeg_release']}.tar.xz,\n"
            f"  plus the fixes on its release branch up to that commit)\n"
            f"Build scripts: {p['builds_repo']}/tree/{p['builds_commit']}\n")


def write_ffmpeg_version(dest: Path, key: str) -> None:
    (dest / FFMPEG_VERSION_NAME).write_text(ffmpeg_version_text(key), "utf-8")
