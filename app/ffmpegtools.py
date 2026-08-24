"""FFmpeg capability probing and encoder presets.

yt-dlp shells out to ffmpeg for every conversion, and accepts raw ffmpeg
arguments per postprocessor. That means hardware encoding, loudness
normalisation and codec choice are all reachable without writing any
encoding code ourselves -- we just hand ffmpeg the right flags.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache

from . import config

# Re-encode targets, best-first. Availability is probed at runtime, so a machine
# without an NVIDIA card simply never sees the nvenc entries.
ENCODERS = [
    {"id": "av1_nvenc", "label": "AV1 (NVIDIA GPU)", "vcodec": "av1_nvenc",
     "note": "smallest files, needs RTX 40-series or newer", "hw": True, "container": "mkv"},
    {"id": "hevc_nvenc", "label": "H.265 / HEVC (NVIDIA GPU)", "vcodec": "hevc_nvenc",
     "note": "about half the size of H.264", "hw": True, "container": "mp4"},
    {"id": "h264_nvenc", "label": "H.264 (NVIDIA GPU)", "vcodec": "h264_nvenc",
     "note": "fast, plays everywhere", "hw": True, "container": "mp4"},
    {"id": "hevc_qsv", "label": "H.265 (Intel Quick Sync)", "vcodec": "hevc_qsv",
     "note": "Intel iGPU acceleration", "hw": True, "container": "mp4"},
    {"id": "h264_qsv", "label": "H.264 (Intel Quick Sync)", "vcodec": "h264_qsv",
     "note": "Intel iGPU acceleration", "hw": True, "container": "mp4"},
    {"id": "hevc_amf", "label": "H.265 (AMD)", "vcodec": "hevc_amf",
     "note": "AMD GPU acceleration", "hw": True, "container": "mp4"},
    {"id": "h264_amf", "label": "H.264 (AMD)", "vcodec": "h264_amf",
     "note": "AMD GPU acceleration", "hw": True, "container": "mp4"},
    {"id": "libx264", "label": "H.264 (CPU)", "vcodec": "libx264",
     "note": "works anywhere, slower", "hw": False, "container": "mp4"},
    {"id": "libx265", "label": "H.265 (CPU)", "vcodec": "libx265",
     "note": "smaller, much slower", "hw": False, "container": "mp4"},
]

QUALITY = {          # roughly comparable perceptual targets
    "high": {"cq": "20", "crf": "18"},
    "balanced": {"cq": "26", "crf": "23"},
    "small": {"cq": "32", "crf": "28"},
}


def _exe(name: str) -> str:
    import os
    from pathlib import Path
    d = config.ffmpeg_dir()          # may be the bundled bin/ or a downloaded pack
    if d:
        return str(Path(d) / (f"{name}.exe" if os.name == "nt" else name))
    return name


@lru_cache(maxsize=1)
def available_encoders() -> list[str]:
    try:
        out = subprocess.run([_exe("ffmpeg"), "-hide_banner", "-encoders"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=25,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stdout + out.stderr
    except Exception:
        return []
    return [e["id"] for e in ENCODERS if e["vcodec"] in text]


def encoder_catalog() -> list[dict]:
    have = set(available_encoders())
    return [{**e, "available": e["id"] in have} for e in ENCODERS if e["id"] in have]


def recode_args(encoder_id: str, quality: str = "balanced",
                normalize_audio: bool = False) -> list[str]:
    """ffmpeg arguments for FFmpegVideoConvertor."""
    spec = next((e for e in ENCODERS if e["id"] == encoder_id), None)
    if not spec:
        return []
    q = QUALITY.get(quality, QUALITY["balanced"])
    args = ["-c:v", spec["vcodec"]]
    if spec["vcodec"].endswith("_nvenc"):
        # p4-p6 are the quality-oriented NVENC presets; vbr + cq is the modern
        # quality-targeted mode and keeps file size sane.
        args += ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", q["cq"], "-b:v", "0"]
    elif spec["vcodec"].endswith("_qsv"):
        args += ["-global_quality", q["cq"], "-preset", "medium"]
    elif spec["vcodec"].endswith("_amf"):
        args += ["-quality", "quality", "-rc", "cqp", "-qp_i", q["cq"], "-qp_p", q["cq"]]
    else:
        args += ["-preset", "medium", "-crf", q["crf"]]
    args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    args += audio_args(normalize_audio)
    return args


def audio_args(normalize: bool) -> list[str]:
    if normalize:
        # EBU R128 single-pass; -14 LUFS is the streaming-platform convention.
        return ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "aac", "-b:a", "192k"]


def summary() -> dict:
    have = available_encoders()
    return {
        "encoders": encoder_catalog(),
        "hardware": [e for e in have if e.endswith(("_nvenc", "_qsv", "_amf"))],
        "qualities": list(QUALITY.keys()),
    }
