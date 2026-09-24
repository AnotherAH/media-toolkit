"""FFmpeg capability probing and encoder presets.

yt-dlp shells out to ffmpeg for every conversion, and accepts raw ffmpeg
arguments per postprocessor. That means hardware encoding, loudness
normalisation and codec choice are all reachable without writing any
encoding code ourselves: we just hand ffmpeg the right flags.

Hardware encoders are proven, not assumed. `ffmpeg -encoders` only says what
the build was compiled with: this ffmpeg lists Intel Quick Sync on an AMD
machine and NVENC on a PC with no NVIDIA card. Each one is therefore asked to
encode a single blank frame, and only the ones that succeed are offered.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Re-encode targets, best-first. "codec" is the family, used for the file tag,
# the container check and the quality table.
ENCODERS = [
    {"id": "av1_nvenc", "label": "NVIDIA AV1 (fast, small files, RTX 40 or newer)",
     "vcodec": "av1_nvenc", "codec": "av1", "note": "small files, needs an RTX 40 series or newer",
     "hw": True},
    {"id": "hevc_nvenc", "label": "NVIDIA H.265 (fast, small files)", "vcodec": "hevc_nvenc",
     "codec": "hevc", "note": "about half the size of H.264", "hw": True},
    {"id": "h264_nvenc", "label": "NVIDIA H.264 (fast, plays everywhere)", "vcodec": "h264_nvenc",
     "codec": "h264", "note": "fast, plays everywhere", "hw": True},
    {"id": "hevc_qsv", "label": "Intel H.265 (fast, small files)", "vcodec": "hevc_qsv",
     "codec": "hevc", "note": "Intel graphics", "hw": True},
    {"id": "h264_qsv", "label": "Intel H.264 (fast, plays everywhere)", "vcodec": "h264_qsv",
     "codec": "h264", "note": "Intel graphics", "hw": True},
    {"id": "hevc_amf", "label": "AMD H.265 (fast, small files)", "vcodec": "hevc_amf",
     "codec": "hevc", "note": "AMD graphics", "hw": True},
    {"id": "h264_amf", "label": "AMD H.264 (fast, plays everywhere)", "vcodec": "h264_amf",
     "codec": "h264", "note": "AMD graphics", "hw": True},
    {"id": "libx264", "label": "H.264 on the processor (slower, plays everywhere)",
     "vcodec": "libx264", "codec": "h264", "note": "works anywhere, slower", "hw": False},
    {"id": "libx265", "label": "H.265 on the processor (much slower, small files)",
     "vcodec": "libx265", "codec": "hevc", "note": "smaller, much slower", "hw": False},
]

# Perceptual targets per codec family. AV1 and HEVC reach the same quality at a
# higher number than H.264, so one shared table made "AV1 balanced" the largest
# output of all.
QUALITY = {
    "high": {"h264": 20, "hevc": 22, "av1": 26, "crf264": 18, "crf265": 20},
    "balanced": {"h264": 24, "hevc": 27, "av1": 32, "crf264": 22, "crf265": 25},
    "small": {"h264": 29, "hevc": 31, "av1": 38, "crf264": 27, "crf265": 29},
}

# Audio bitrate follows the video choice: "Small file" should not spend 192k on sound.
AUDIO_BITRATE = {"high": ("192k", "160k"), "balanced": ("160k", "128k"), "small": ("128k", "96k")}

# EBU R128 to -14 LUFS, the level streaming services play at. loudnorm
# resamples to 192 kHz internally, hence the explicit output rate.
LOUDNORM_TARGET = "I=-14:TP=-1.5:LRA=11"
# One pass, for when the sound could not be measured first. It guesses the
# level from the first few seconds and lands 2 to 3 LU off on short clips.
LOUDNORM = ["-af", f"loudnorm={LOUDNORM_TARGET}", "-ar", "48000"]
_LOUDNESS_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def loudness_scan_args() -> list[str]:
    """Output arguments for the measuring pass (the first of two)."""
    return ["-map", "0:a:0", "-af", f"loudnorm={LOUDNORM_TARGET}:print_format=json",
            "-f", "null"]


def parse_loudness(stderr: str) -> dict | None:
    """The measurements loudnorm prints after a scan, or None (silence
    measures as -inf, which the second pass cannot use)."""
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr or "", re.S)
    if not found:
        return None
    try:
        data = json.loads(found[-1])
        values = {k: float(data[k]) for k in _LOUDNESS_KEYS}
    except (ValueError, KeyError, TypeError):
        return None
    return values if all(math.isfinite(v) for v in values.values()) else None


def loudnorm_args(measured: dict | None = None) -> list[str]:
    """The loudness filter: two-pass with the measurements when there are
    some, which lands within about half a LU of the target."""
    if not measured:
        return list(LOUDNORM)
    f = (f"loudnorm={LOUDNORM_TARGET}:measured_I={measured['input_i']:.2f}"
         f":measured_TP={measured['input_tp']:.2f}:measured_LRA={measured['input_lra']:.2f}"
         f":measured_thresh={measured['input_thresh']:.2f}:offset={measured['target_offset']:.2f}"
         ":linear=true")
    return ["-af", f, "-ar", "48000"]

# Which codecs each container can hold, for choosing copy versus re-encode.
_CONTAINER_VIDEO = {"mp4": {"h264", "hevc", "av1"}, "mov": {"h264", "hevc"},
                    "mkv": {"h264", "hevc", "av1"}, "webm": {"av1"}}
_CONTAINER_AUDIO_COPY = {"mp4": {"aac", "mp3", "alac"}, "mov": {"aac", "alac"},
                         "mkv": None, "webm": {"opus", "vorbis"}}   # None: anything goes


def exe(name: str) -> str:
    """Path of ffmpeg or ffprobe: bundled bin/, a downloaded pack, or PATH."""
    d = config.ffmpeg_dir()
    if d:
        return str(Path(d) / (f"{name}.exe" if os.name == "nt" else name))
    return name


_exe = exe   # older name, kept for callers outside this module


def ffmpeg_available() -> bool:
    """True when an ffmpeg we can run exists (bundled, repaired, or on PATH)."""
    if config.ffmpeg_dir():
        return True
    import shutil
    return bool(shutil.which("ffmpeg"))


# ------------------------------------------------------------------ encoders

_cache_lock = threading.Lock()
_cache: dict[tuple, list[str]] = {}


def _ffmpeg_key() -> tuple:
    """Identity of the ffmpeg in use, so a repaired or replaced binary re-probes."""
    path = exe("ffmpeg")
    try:
        st = os.stat(path)
        return (path, st.st_size, int(st.st_mtime))
    except OSError:
        return (path, 0, 0)


def _compiled_in(ffmpeg: str) -> str:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                             timeout=25, creationflags=_NO_WINDOW)
        return out.stdout + out.stderr
    except Exception:
        return ""


def test_encode(ffmpeg: str, vcodec: str, timeout: float = 20.0) -> bool:
    """Encode one small blank frame. A driverless or absent GPU fails here.

    320x240 keeps every encoder above its minimum frame size (NVENC and AMF
    refuse tiny frames), and yuv420p is the one pixel format all of them take.
    """
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=0.1",
           "-frames:v", "1", "-pix_fmt", "yuv420p", "-c:v", vcodec, "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                             timeout=timeout, creationflags=_NO_WINDOW)
        return out.returncode == 0
    except Exception:
        return False


def available_encoders(refresh: bool = False) -> list[str]:
    """Encoder ids that really work on this machine, best first.

    Cached per ffmpeg binary (path, size, date), so installing or repairing
    ffmpeg is picked up without a restart. `refresh=True` re-tests, e.g. after
    a graphics driver update.
    """
    key = _ffmpeg_key()
    with _cache_lock:
        if not refresh and key in _cache:
            return list(_cache[key])
    ffmpeg = key[0]
    listed = _compiled_in(ffmpeg)
    candidates = [e for e in ENCODERS if e["vcodec"] in listed]
    if candidates:
        # One frame each, side by side: about a second in total instead of seven.
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            ok = list(pool.map(lambda e: test_encode(ffmpeg, e["vcodec"]), candidates))
        working = [e["id"] for e, good in zip(candidates, ok) if good]
    else:
        working = []
    with _cache_lock:
        _cache.clear()
        _cache[key] = working
    return list(working)


def refresh() -> list[str]:
    """Forget the cached encoder list and test again."""
    with _cache_lock:
        _cache.clear()
    return available_encoders(refresh=True)


def warm_up() -> None:
    """Run the encoder test in the background so the first Settings visit is instant."""
    threading.Thread(target=available_encoders, name="encoder-probe", daemon=True).start()


def encoder_catalog() -> list[dict]:
    have = set(available_encoders())
    return [{**e, "available": True} for e in ENCODERS if e["id"] in have]


def spec(encoder_id: str) -> dict | None:
    return next((e for e in ENCODERS if e["id"] == encoder_id), None)


def recode_container(encoder_id: str, container: str) -> str:
    """The container the re-encoded file goes into: the user's choice when it
    can hold the codec, otherwise MKV, which holds everything."""
    s = spec(encoder_id)
    container = (container or "mp4").lower()
    if not s:
        return container if container in _CONTAINER_VIDEO else "mp4"
    if s["codec"] in _CONTAINER_VIDEO.get(container, set()):
        return container
    return "mkv"


def video_args(encoder_id: str, quality: str = "balanced", container: str = "mp4") -> list[str]:
    """ffmpeg video arguments for one encoder and quality level."""
    s = spec(encoder_id)
    if not s:
        return []
    q = QUALITY.get(quality, QUALITY["balanced"])
    cq = str(q[s["codec"]])
    v = s["vcodec"]
    args = ["-c:v", v]
    if v.endswith("_nvenc"):
        # p5 is the quality end of NVENC's presets; vbr + cq targets quality and
        # keeps file size proportional to the content.
        args += ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0"]
    elif v.endswith("_qsv"):
        args += ["-global_quality", cq, "-preset", "medium"]
    elif v.endswith("_amf"):
        args += ["-quality", "quality", "-rc", "cqp", "-qp_i", cq, "-qp_p", cq]
    elif v == "libx265":
        args += ["-preset", "medium", "-crf", str(q["crf265"])]
    else:
        args += ["-preset", "medium", "-crf", str(q["crf264"])]
    args += ["-pix_fmt", "yuv420p"]
    if container in ("mp4", "mov"):
        if s["codec"] == "hevc":
            # ffmpeg tags HEVC as hev1 by default; Apple devices only play hvc1.
            args += ["-tag:v", "hvc1"]
        args += ["-movflags", "+faststart"]
    return args


def audio_args(container: str = "mp4", source_codec: str | None = None,
               normalize: bool = False, quality: str = "balanced",
               loudness: dict | None = None) -> list[str]:
    """Audio arguments for a re-encode or a loudness pass.

    Copies the original sound when it already fits the container and nothing
    has to change; otherwise re-encodes once, at a bitrate that follows the
    chosen quality. ``loudness`` holds a measuring pass's results.
    """
    container = (container or "mp4").lower()
    src = (source_codec or "").lower()
    allowed = _CONTAINER_AUDIO_COPY.get(container, set())
    if not normalize and src and (allowed is None or src in allowed):
        return ["-c:a", "copy"]
    aac_rate, opus_rate = AUDIO_BITRATE.get(quality, AUDIO_BITRATE["balanced"])
    # Sound-only files keep their own format (a podcast .mp3 stays .mp3), and
    # each of those containers takes exactly one kind of sound.
    if container == "mp3":
        enc = ["-c:a", "libmp3lame", "-b:a", aac_rate]
    elif container == "flac":
        enc = ["-c:a", "flac"]
    elif container == "wav":
        enc = ["-c:a", "pcm_s16le"]
    elif container in ("webm", "opus", "ogg", "oga") or (container == "mkv" and src in ("opus", "vorbis")):
        enc = ["-c:a", "libopus", "-b:a", opus_rate]
    else:
        enc = ["-c:a", "aac", "-b:a", aac_rate]
    return (loudnorm_args(loudness) if normalize else []) + enc


def recode_args(encoder_id: str, quality: str = "balanced",
                normalize_audio: bool = False, container: str = "mp4") -> list[str]:
    """Video plus audio arguments when the source audio codec is not known yet."""
    if not spec(encoder_id):
        return []
    return video_args(encoder_id, quality, container) + audio_args(
        container, None, normalize_audio, quality)


# ------------------------------------------------------------------- probing

def probe(path: str, timeout: float = 30.0) -> dict:
    """Duration and main stream facts of a media file, via ffprobe.

    Returns {} when the file cannot be read, so callers can treat "unknown"
    and "different" the same way.
    """
    cmd = [exe("ffprobe"), "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,width,height",
           "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                             timeout=timeout, creationflags=_NO_WINDOW, encoding="utf-8",
                             errors="replace")
        data = json.loads(out.stdout or "{}")
    except Exception:
        return {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("codec_name") not in ("mjpeg", "png", "bmp")), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    return {"duration": duration, "height": video.get("height") or 0,
            "width": video.get("width") or 0, "vcodec": video.get("codec_name") or "",
            "acodec": audio.get("codec_name") or ""}


def summary() -> dict:
    have = available_encoders()
    return {
        "encoders": encoder_catalog(),
        "hardware": [e for e in have if e.endswith(("_nvenc", "_qsv", "_amf"))],
        "qualities": list(QUALITY.keys()),
    }
