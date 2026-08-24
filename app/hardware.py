"""GPU detection and Whisper backend selection.

Works on any machine: Blackwell (RTX 50xx), Ada/Ampere/Turing (RTX 40xx/30xx/20xx),
Pascal (GTX 10xx), non-NVIDIA GPUs, and CPU-only boxes. Nothing is hardcoded to one
card -- we detect compute capability + VRAM, build an ordered list of candidate
backends, then *prove* one works by running a real warm-up inference before using it.
"""
from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from . import config

# Approximate VRAM (MB) a model needs, per compute type. Used to skip candidates
# that would OOM and to warn before a large model is picked on a small card.
MODEL_VRAM = {
    "tiny": 400, "tiny.en": 400,
    "base": 600, "base.en": 600,
    "small": 1400, "small.en": 1400,
    "medium": 3200, "medium.en": 3200,
    "large-v1": 5400, "large-v2": 5400, "large-v3": 5400,
    "distil-small.en": 900, "distil-medium.en": 1900,
    "distil-large-v2": 3000, "distil-large-v3": 3000,
    "large-v3-turbo": 2400, "turbo": 2400,
}
COMPUTE_FACTOR = {"float32": 2.0, "float16": 1.0, "int8_float16": 0.6, "int8": 0.55}

CACHE_PATH = config.DATA_ROOT / ".backend-cache.json"


@lru_cache(maxsize=1)
def probe_gpus() -> list[dict]:
    """Enumerate NVIDIA GPUs via nvidia-smi. Empty list = no usable NVIDIA GPU."""
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
            gpus.append({"name": parts[0], "compute_cap": cap, "vram_mb": int(float(parts[2]))})
        return gpus
    except Exception:
        return []


def cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _tier_order(cap: float) -> list[str]:
    """Preferred compute types for a given CUDA compute capability, fastest first.

    int8 kernels on Blackwell (12.x) hit a cuBLAS path that older CTranslate2
    builds mis-pad, so fp16 leads there. Turing..Ada (7.5-8.9) have mature INT8
    tensor cores, so int8_float16 leads -- it is both the fastest and the
    smallest, which matters on 8-10GB cards like the 3070/3080.
    Pascal (6.x) has no usable fp16 throughput, so it goes int8 then fp32.
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
    return int(base * COMPUTE_FACTOR.get(compute, 1.0)) + 700  # + cuDNN/ctx overhead


def candidates(model: str, device_pref: str = "auto", compute_pref: str = "auto") -> list[tuple[str, str]]:
    """Ordered (device, compute_type) pairs to try for this model on this machine."""
    out: list[tuple[str, str]] = []
    gpus = probe_gpus()

    if device_pref != "cpu" and gpus and cuda_available():
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

    if device_pref != "cuda":
        cpu_ct = compute_pref if compute_pref in ("int8", "float32") else "int8"
        out.append(("cpu", cpu_ct))
        if cpu_ct != "float32":
            out.append(("cpu", "float32"))
    elif not out:
        out.append(("cpu", "int8"))  # asked for cuda but none usable -- never dead-end
    return out


def _cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def remember(model: str, device: str, compute: str) -> None:
    c = _cache()
    c[model] = {"device": device, "compute_type": compute}
    try:
        CACHE_PATH.write_text(json.dumps(c, indent=2), "utf-8")
    except Exception:
        pass


def recall(model: str) -> tuple[str, str] | None:
    hit = _cache().get(model)
    if hit:
        return hit["device"], hit["compute_type"]
    return None


def forget() -> None:
    CACHE_PATH.unlink(missing_ok=True)


def summary() -> dict:
    """Human-readable hardware report for the Settings screen."""
    gpus = probe_gpus()
    cuda = cuda_available()
    cap = max((g["compute_cap"] for g in gpus), default=0.0)
    vram = max((g["vram_mb"] for g in gpus), default=0)
    if gpus and cuda:
        arch = ("Blackwell" if cap >= 12 else "Ada/Hopper" if cap >= 8.9 else
                "Ampere" if cap >= 8.0 else "Turing" if cap >= 7.5 else
                "Volta" if cap >= 7.0 else "Pascal" if cap >= 6.0 else "legacy")
        note = f"{arch} (sm_{int(cap * 10)}) - GPU transcription enabled"
    elif gpus and not cuda:
        note = "GPU found but CUDA runtime unavailable - using CPU"
    else:
        note = "No NVIDIA GPU detected - using CPU (native subtitles are still instant)"
    return {
        "gpus": gpus,
        "cuda": cuda,
        "compute_cap": cap,
        "vram_mb": vram,
        "cpu_threads": os.cpu_count() or 4,
        "note": note,
        "recommended_model": recommend_model(vram if cuda else 0),
        "ffmpeg": bool(config.ffmpeg_dir()) or _which("ffmpeg"),
        "cached": _cache(),
    }


def _which(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def recommend_model(vram_mb: int) -> str:
    """Best default model for the available VRAM (0 = CPU-only)."""
    if vram_mb >= 8000:
        return "large-v3"
    if vram_mb >= 5000:
        return "large-v3-turbo"
    if vram_mb >= 3000:
        return "medium"
    if vram_mb >= 1800:
        return "small"
    if vram_mb > 0:
        return "base"
    return "small"  # CPU: small is the sweet spot for a many-core desktop
