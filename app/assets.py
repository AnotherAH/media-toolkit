"""On-demand runtime packs, fetched without pip.

The installer stays small by leaving two large pieces out and fetching them only
when they are actually needed:

* **GPU pack** - CUDA cuBLAS, ~740 MB. Only for machines with an NVIDIA card.
  Measured against the alternative: bundling cuDNN as well costs another 1.25 GB
  and changes nothing, because CTranslate2 ships its own cuDNN shim and routes
  Whisper through cuBLAS.
* **ffmpeg** - normally shipped with the installer; this is the repair path.

Wheels are plain zip files, so a URL fetch plus a member extract is all it takes.
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import urllib.request
import zipfile
from pathlib import Path
from threading import Lock

from . import config

PYPI = "https://pypi.org/pypi/{}/json"
FFMPEG_URL = ("https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")

# Everything CTranslate2 actually resolves at runtime for Whisper on CUDA.
CUBLAS_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
FFMPEG_EXES = ("ffmpeg.exe", "ffprobe.exe")

_lock = Lock()
_state: dict = {"busy": False, "task": "", "percent": 0.0, "message": "", "error": ""}


def state() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def gpu_pack_installed() -> bool:
    return all((config.CUDA_DIR / d).exists() for d in CUBLAS_DLLS)


def ffmpeg_installed() -> bool:
    return config.ffmpeg_dir() is not None


def status() -> dict:
    from . import hardware
    gpus = hardware.probe_gpus()
    return {
        "gpu_pack_installed": gpu_pack_installed(),
        "gpu_pack_needed": bool(gpus) and not gpu_pack_installed(),
        "gpu_name": gpus[0]["name"] if gpus else "",
        "gpu_pack_size_mb": 740,
        "ffmpeg_installed": ffmpeg_installed(),
        "progress": state(),
    }


def _download(url: str, label: str, base: float = 0.0, span: float = 100.0) -> bytes:
    """Fetch to memory, reporting percentage into the shared progress state."""
    req = urllib.request.Request(url, headers={"User-Agent": "MediaToolkit"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            buf.write(chunk)
            read += len(chunk)
            pct = (read / total) if total else 0.0
            _set(percent=round(base + span * pct, 1),
                 message=f"{label}: {read / 1048576:.0f} MB"
                         + (f" of {total / 1048576:.0f} MB" if total else ""))
    return buf.getvalue()


def _wheel_url(package: str) -> str:
    with urllib.request.urlopen(PYPI.format(package), timeout=60) as r:
        data = json.loads(r.read())
    tag = {"AMD64": "win_amd64", "ARM64": "win_arm64"}.get(platform.machine(), "win_amd64")
    for entry in data["urls"]:
        name = entry.get("filename", "")
        if name.endswith(".whl") and tag in name:
            return entry["url"]
    raise RuntimeError(f"No Windows wheel published for {package}")


def install_gpu_pack() -> dict:
    """Fetch cuBLAS out of the official NVIDIA wheel."""
    if state()["busy"]:
        return {"ok": False, "error": "Another download is already running."}
    _set(busy=True, task="gpu", percent=0.0, message="Looking up the CUDA package", error="")
    try:
        url = _wheel_url("nvidia-cublas-cu12")
        blob = _download(url, "Downloading CUDA libraries", 0, 92)

        _set(percent=94.0, message="Extracting")
        config.CUDA_DIR.mkdir(parents=True, exist_ok=True)
        found = 0
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if base in CUBLAS_DLLS:
                    with zf.open(member) as src, (config.CUDA_DIR / base).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found += 1
        if found < len(CUBLAS_DLLS):
            raise RuntimeError(f"Wheel only contained {found} of {len(CUBLAS_DLLS)} libraries")

        config.bootstrap()                      # make them loadable in this process
        from . import hardware
        hardware.forget()                       # re-prove the backend next run
        _set(percent=100.0, message="GPU acceleration ready")
        return {"ok": True, "path": str(config.CUDA_DIR), "files": found}
    except Exception as exc:
        _set(error=str(exc)[:300], message="Failed")
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        _set(busy=False)


def install_ffmpeg() -> dict:
    if state()["busy"]:
        return {"ok": False, "error": "Another download is already running."}
    if os.name != "nt":
        return {"ok": False, "error": "Install ffmpeg with your package manager on this platform."}
    _set(busy=True, task="ffmpeg", percent=0.0, message="Downloading ffmpeg", error="")
    try:
        blob = _download(FFMPEG_URL, "Downloading ffmpeg", 0, 92)
        _set(percent=94.0, message="Extracting")
        dest = config.RUNTIME_DIR / "bin"
        dest.mkdir(parents=True, exist_ok=True)
        found = 0
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if base in FFMPEG_EXES:
                    with zf.open(member) as src, (dest / base).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found += 1
        if not found:
            raise RuntimeError("Archive did not contain ffmpeg")
        config.bootstrap()
        _set(percent=100.0, message="ffmpeg ready")
        return {"ok": True, "path": str(dest), "files": found}
    except Exception as exc:
        _set(error=str(exc)[:300], message="Failed")
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        _set(busy=False)


def remove_gpu_pack() -> dict:
    shutil.rmtree(config.CUDA_DIR, ignore_errors=True)
    from . import hardware
    hardware.forget()
    return {"ok": True}
