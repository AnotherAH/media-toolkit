"""GPU detection and Whisper backend selection.

Works on any machine: current and older NVIDIA cards, non-NVIDIA GPUs and
CPU-only boxes. Nothing is hardcoded to one card: we detect compute capability
and VRAM, build an ordered list of candidate backends, then *prove* one works by
running a real warm-up inference before using it.

"GPU ready" means more than "an NVIDIA driver answers". CTranslate2 only needs
the driver to count devices, but Whisper on CUDA also needs cuBLAS, which the
installed app gets from the optional GPU pack. Reporting the driver alone made
the app promise GPU speed and then quietly run the largest model on the CPU.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from . import config

# Approximate VRAM (MB) a model needs, per compute type. Used to skip candidates
# that would OOM and to warn before a large model is picked on a small card.
MODEL_VRAM = {
    "tiny": 400, "tiny.en": 400,
    "base": 600, "base.en": 600,
    "small": 1400, "small.en": 1400,
    "medium": 3200, "medium.en": 3200,
    "large-v1": 5400, "large-v2": 5400, "large-v3": 5400, "large": 5400,
    "distil-small.en": 900, "distil-medium.en": 1900,
    "distil-large-v2": 3000, "distil-large-v3": 3000, "distil-large-v3.5": 3000,
    "large-v3-turbo": 2400, "turbo": 2400,
}
COMPUTE_FACTOR = {"float32": 2.0, "float16": 1.0, "int8_float16": 0.6, "int8": 0.55}
DEVICES = ("auto", "cuda", "cpu")
COMPUTE_TYPES = ("auto", "float16", "int8_float16", "int8", "float32")

# What Whisper on CUDA loads at run time, and the default GPU pack size.
CUBLAS_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
GPU_PACK_SIZE_MB = 740

CACHE_PATH = config.DATA_ROOT / ".backend-cache.json"

# Probing CUDA is done once per process and never from two threads at once:
# the first call initialises the driver, and on a busy GPU that call has been
# seen to crash the whole process when made from a worker thread.
_probe_lock = threading.Lock()
_gpus: list[dict] | None = None
_cuda_count: int | None = None
_cuda_failed_this_session = False


def probe_gpus() -> list[dict]:
    """Enumerate NVIDIA GPUs via nvidia-smi. Empty list = no usable NVIDIA GPU."""
    global _gpus
    with _probe_lock:
        if _gpus is None:
            _gpus = _nvidia_smi()
        return [dict(g) for g in _gpus]


def _nvidia_smi() -> list[dict]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,compute_cap,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            return []
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                cap = float(parts[1])
            except ValueError:
                cap = 0.0
            try:
                vram = int(float(parts[2]))
            except ValueError:
                vram = 0
            gpus.append({"name": parts[0], "compute_cap": cap, "vram_mb": vram})
        return gpus
    except Exception:
        return []


def cuda_device_count() -> int:
    """CUDA devices CTranslate2 can see (driver level), probed once."""
    global _cuda_count
    with _probe_lock:
        if _cuda_count is None:
            try:
                import ctranslate2
                _cuda_count = int(ctranslate2.get_cuda_device_count())
            except Exception:
                _cuda_count = 0
        return _cuda_count


def cuda_available() -> bool:
    return cuda_device_count() > 0


def prime() -> None:
    """Probe the hardware now. Meant for the main thread at startup, so the
    first CUDA initialisation never happens inside a request handler."""
    probe_gpus()
    cuda_device_count()


def cublas_dir() -> str:
    """Folder that holds a loadable cuBLAS for CUDA 12, or ''.

    Looks where Windows will look when CTranslate2 loads it: the GPU pack
    folder, a CUDA toolkit on PATH, or the pip wheels of a source checkout
    (config.bootstrap puts those on PATH). The files are checked, not loaded,
    so the GPU pack can still be removed later in the session.
    """
    if os.name != "nt":
        return "system"      # Linux/macOS builds resolve CUDA through the loader
    dirs = [config.CUDA_DIR]
    dirs += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]
    for d in dirs:
        try:
            if all((d / name).is_file() for name in CUBLAS_DLLS):
                return str(d)
        except OSError:
            continue
    return ""


def gpu_pack_installed() -> bool:
    try:
        from . import assets
        return bool(assets.gpu_pack_installed())
    except Exception:
        return all((config.CUDA_DIR / d).exists() for d in CUBLAS_DLLS)


def gpu_pack_size_mb() -> int:
    try:
        from . import assets
        return int(getattr(assets, "GPU_PACK_SIZE_MB", GPU_PACK_SIZE_MB))
    except Exception:
        return GPU_PACK_SIZE_MB


def cublas_ready() -> bool:
    """cuBLAS is where CTranslate2 will find it: the GPU pack, a CUDA toolkit,
    or the pip wheels of a source checkout."""
    return bool(cublas_dir()) or gpu_pack_installed()


def gpu_ready() -> bool:
    """Whisper will really run on the NVIDIA card: a card, a working driver,
    cuBLAS where Windows can load it, and no CUDA failure this session."""
    if _cuda_failed_this_session or not probe_gpus():
        return False
    return cublas_ready() and cuda_available()


def mark_cuda_failed() -> None:
    """CUDA broke in the middle of a job. Use the processor for the rest of
    this session, so 'Try again' actually works; Re-check hardware or a
    restart gives the GPU another chance."""
    global _cuda_failed_this_session
    _cuda_failed_this_session = True


def cuda_failed() -> bool:
    """CUDA broke earlier this session (see mark_cuda_failed)."""
    return _cuda_failed_this_session


def _tier_order(cap: float) -> list[str]:
    """Preferred compute types for a given CUDA compute capability, fastest first.

    int8 kernels on compute capability 12.x hit a cuBLAS path that older
    CTranslate2 builds mis-pad, so fp16 leads there. 7.5 to 8.9 have mature
    INT8 tensor cores, so int8_float16 leads: it is both the fastest and the
    smallest, which matters on 8-10 GB cards. 6.x has no usable fp16
    throughput, so it goes int8 then fp32.
    """
    if cap >= 12.0:
        return ["float16", "int8_float16", "int8", "float32"]
    if cap >= 7.5:
        return ["int8_float16", "float16", "int8", "float32"]
    if cap >= 7.0:
        return ["float16", "int8_float16", "int8", "float32"]
    if cap >= 6.0:
        return ["int8", "float32", "float16"]
    return ["float32"]


def estimated_vram(model: str, compute: str) -> int:
    base = MODEL_VRAM.get(model, 5400)
    return int(base * COMPUTE_FACTOR.get(compute, 1.0)) + 700  # + context overhead


def candidates(model: str, device_pref: str = "auto", compute_pref: str = "auto") -> list[tuple[str, str]]:
    """Ordered (device, compute_type) pairs to try for this model on this machine."""
    device_pref = device_pref if device_pref in DEVICES else "auto"
    compute_pref = compute_pref if compute_pref in COMPUTE_TYPES else "auto"
    out: list[tuple[str, str]] = []
    gpus = probe_gpus()

    if device_pref != "cpu" and gpus and gpu_ready():
        cap = max(g["compute_cap"] for g in gpus)
        vram = max(g["vram_mb"] for g in gpus)
        order = [compute_pref] if compute_pref != "auto" else _tier_order(cap)
        for ct in order:
            # Skip a candidate that clearly will not fit; a smaller quant may still fit.
            if compute_pref == "auto" and estimated_vram(model, ct) > vram:
                continue
            out.append(("cuda", ct))
        if not out and compute_pref == "auto":
            out.append(("cuda", "int8"))  # last-ditch: smallest footprint

    if device_pref != "cuda" or not out:
        # Asked for CUDA but none is usable: never dead-end, use the processor.
        cpu_ct = compute_pref if compute_pref in ("int8", "float32") else "int8"
        out.append(("cpu", cpu_ct))
        if cpu_ct != "float32":
            out.append(("cpu", "float32"))
    return out


_cache_lock = threading.Lock()


def _cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text("utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def remember(model: str, device: str, compute: str) -> None:
    """Save the backend that worked. Written to a temporary file and swapped
    in, so a crash or a full disk mid-write never leaves a broken file."""
    with _cache_lock:
        c = _cache()
        c[model] = {"device": device, "compute_type": compute}
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        try:
            tmp.write_text(json.dumps(c, indent=2), "utf-8")
            os.replace(tmp, CACHE_PATH)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def recall(model: str) -> tuple[str, str] | None:
    hit = _cache().get(model)
    if isinstance(hit, dict) and hit.get("device") and hit.get("compute_type"):
        return hit["device"], hit["compute_type"]
    return None


def forget() -> None:
    """Re-prove everything: the remembered backends, the GPU list, the CUDA
    probe and any CUDA failure this session. Called after the GPU pack is
    installed or removed, after an engine setting changes, and by Re-check
    hardware."""
    global _gpus, _cuda_count, _cuda_failed_this_session
    CACHE_PATH.unlink(missing_ok=True)
    with _probe_lock:
        _gpus = None
        _cuda_count = None
        _cuda_failed_this_session = False


def summary() -> dict:
    """One hardware truth for every screen.

    nvidia_name and gpu_ready drive what the UI says; the older fields stay for
    compatibility. The note never names a GPU architecture.
    """
    gpus = probe_gpus()
    best = max(gpus, key=lambda g: g["vram_mb"]) if gpus else None
    name = best["name"] if best else ""
    ready = gpu_ready()
    cap = max((g["compute_cap"] for g in gpus), default=0.0)
    vram = max((g["vram_mb"] for g in gpus), default=0)
    threads = os.cpu_count() or 4
    if ready:
        note = f"Transcription runs on your {name}. Fast."
    elif name:
        note = ("Transcription runs on your processor. "
                f"Your {name} can do it many times faster after a one-time "
                f"{gpu_pack_size_mb()} MB download.")
    else:
        note = (f"Transcription runs on your processor ({threads} threads). "
                "Videos with captions are still instant.")
    return {
        "gpus": gpus,
        "nvidia_name": name,
        "gpu_ready": ready,
        "gpu_pack_installed": gpu_pack_installed(),
        "gpu_pack_size_mb": gpu_pack_size_mb(),
        "cuda": ready,                       # legacy: "Whisper will use CUDA"
        # nvidia-smi answering means the driver is installed. Asking CUDA
        # itself is left to gpu_ready(), which only does it once cuBLAS is
        # there: without it the answer changes nothing, and each first probe
        # from a request thread is a chance to crash on a busy card.
        "cuda_driver": bool(gpus),
        "compute_cap": cap,
        "vram_mb": vram,
        "cpu_threads": threads,
        "note": note,
        "recommended_model": recommend_model(vram if ready else 0),
        "ffmpeg": bool(config.ffmpeg_dir()) or _which("ffmpeg"),
        "cached": _cache(),
    }


def _which(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def recommend_model(vram_mb: int) -> str:
    """Best default model for the VRAM Whisper can actually use (0 = processor).

    Large v3 Turbo is the pick for any card with room for it: close to Large
    v3's accuracy at several times the speed, and half the download.
    """
    if vram_mb >= 5000:
        return "large-v3-turbo"
    if vram_mb >= 3000:
        return "medium"
    if vram_mb >= 1800:
        return "small"
    if vram_mb > 0:
        return "base"
    return "small"  # processor: small is the sweet spot for a many-core desktop
