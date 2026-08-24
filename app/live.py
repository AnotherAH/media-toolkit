"""Live stream recording.

yt-dlp can download a live stream, but it hands the job to ffmpeg internally and
gives nothing back: no progress callbacks fire, and there is no way to stop the
recording and keep what you have. For live that is the whole feature, so this
module drives ffmpeg itself.

yt-dlp still does the hard part -- resolving the stream manifest and the headers
needed to fetch it -- which is why this works on every site it supports, not just
YouTube. We then run ffmpeg with `-c copy`, so nothing is re-encoded: the video
and audio are muxed together into one file as they arrive.

Recording into MPEG-TS matters. It is a stream container with no index to write
at the end, so a recording that is stopped, crashes, or loses the network is
still a complete, playable file up to that point. It is remuxed to MP4 when the
recording finishes.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from yt_dlp import YoutubeDL

from . import config, ffmpegtools, jobs, media

# Live HTTP streams drop connections; without these ffmpeg gives up on the first
# blip and a long recording never survives the night.
RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "30", "-rw_timeout", "15000000"]

QUALITY_SELECTORS = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",
    "audio": "ba/b",
}


# --------------------------------------------------------------------- resolve

def resolve(url: str, quality: str = "best", audio_only: bool = False) -> dict:
    """Ask yt-dlp for the stream URLs and headers. Works for any supported site."""
    selector = QUALITY_SELECTORS.get("audio" if audio_only else quality,
                                     QUALITY_SELECTORS["best"])
    opts = media.base_opts()
    opts.update({"noplaylist": True, "skip_download": True, "format": selector})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Nothing to record at that link.")
        info = entries[0]

    entries = info.get("requested_formats") or [info]
    video = next((f for f in entries if _has_video(f)), None)
    audio = next((f for f in entries if f is not video and _has_audio(f)), None)
    single = len(entries) == 1 and _has_video(entries[0]) and _has_audio(entries[0])

    if audio_only:
        video = None
        audio = audio or (entries[0] if entries else None)
        single = False
    elif video and not audio and not single:
        # The video rendition carries no sound and nothing was paired with it.
        # Resolve the audio track separately so the recording is never silent.
        audio = _best_audio(url)

    if not video and not audio:
        raise RuntimeError("No playable stream found at that link.")

    return {
        "is_live": bool(info.get("is_live")),
        "was_live": bool(info.get("was_live")),
        "live_status": info.get("live_status") or "",
        "title": info.get("title") or "Live recording",
        "meta": media.summarize(info),
        "video": _target(video),
        "audio": _target(audio) if (audio and audio is not video) else None,
        "single": bool(video and audio and video is audio),
    }


def _has_video(fmt: dict) -> bool:
    return bool(fmt.get("height")) or (fmt.get("vcodec") not in (None, "none"))


def _has_audio(fmt: dict) -> bool:
    """Codec fields are unreliable on live HLS: renditions often report acodec as
    None (unknown) rather than a codec name. Treat "no picture" as audio."""
    acodec = fmt.get("acodec")
    if acodec == "none":
        return False
    if acodec:
        return True
    return not fmt.get("height") and fmt.get("vcodec") in (None, "none")


def _best_audio(url: str) -> dict | None:
    opts = media.base_opts()
    opts.update({"noplaylist": True, "skip_download": True, "format": "ba/b"})
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else {}
    picked = (info.get("requested_formats") or [info])[0]
    return picked if picked.get("url") else None


def _target(fmt: dict | None) -> dict | None:
    if not fmt:
        return None
    return {"url": fmt.get("url", ""), "headers": fmt.get("http_headers") or {},
            "ext": fmt.get("ext", ""), "protocol": fmt.get("protocol", ""),
            "height": fmt.get("height") or 0, "format_id": fmt.get("format_id", "")}


def wait_until_live(url: str, jid: str | None, timeout_minutes: int = 180) -> dict:
    """Poll a scheduled or offline stream until it starts."""
    deadline = time.time() + timeout_minutes * 60
    delay, attempt = 15, 0
    while time.time() < deadline:
        if jid:
            jobs.raise_if_cancelled(jid)
        try:
            info = resolve(url)
            if info["is_live"] or info["video"] or info["audio"]:
                return info
        except Exception as exc:
            if "not currently live" not in str(exc).lower() and attempt > 6:
                raise
        attempt += 1
        if jid:
            jobs.update(jid, stage=f"Waiting for the stream to start (checked {attempt}x)")
        for _ in range(int(delay / 0.5)):
            if jid:
                jobs.raise_if_cancelled(jid)
            time.sleep(0.5)
        delay = min(delay * 1.5, 90)
    raise RuntimeError("The stream did not start within the time limit.")


# --------------------------------------------------------------------- record

def _header_blob(headers: dict) -> list[str]:
    """ffmpeg wants one CRLF-joined string; User-Agent goes in its own flag."""
    args: list[str] = []
    ua = headers.get("User-Agent") or headers.get("user-agent")
    if ua:
        args += ["-user_agent", ua]
    rest = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    if rest:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in rest.items())]
    return args


def build_command(target: dict, out_path: Path, o: dict) -> list[str]:
    exe = str(Path(config.ffmpeg_dir() or "") / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")) \
        if config.ffmpeg_dir() else "ffmpeg"
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-progress", "pipe:1"]

    video, audio = target.get("video"), target.get("audio")
    inputs = 0
    if video:
        cmd += _header_blob(video["headers"]) + RECONNECT + ["-i", video["url"]]
        inputs += 1
    if audio:
        cmd += _header_blob(audio["headers"]) + RECONNECT + ["-i", audio["url"]]
        inputs += 1

    if video and audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    elif video:
        cmd += ["-map", "0"]
    else:
        cmd += ["-map", "0:a"]

    cmd += ["-c", "copy"]
    if o.get("max_minutes"):
        cmd += ["-t", str(int(float(o["max_minutes"]) * 60))]

    split = int(o.get("split_minutes") or 0)
    if split > 0:
        cmd += ["-f", "segment", "-segment_time", str(split * 60),
                "-reset_timestamps", "1", "-segment_format", "mpegts",
                str(out_path.with_name(out_path.stem + " part%03d.ts"))]
    else:
        cmd += ["-f", "mpegts", "-y", str(out_path)]
    return cmd


_PROGRESS = re.compile(r"^(\w+)=(.*)$")


def _run_ffmpeg(cmd: list[str], jid: str, on_tick) -> tuple[int, str]:
    """Run ffmpeg, stream progress out, and stop gracefully when cancelled."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)

    stats: dict[str, str] = {}
    stopping = False
    try:
        for line in proc.stdout:
            if (m := _PROGRESS.match(line.strip())):
                stats[m.group(1)] = m.group(2)
                if m.group(1) == "progress":
                    on_tick(stats)
            if not stopping and jobs.cancelled(jid):
                stopping = True
                # "q" is ffmpeg's graceful quit: it finalises the file properly
                # instead of leaving a truncated one behind.
                try:
                    proc.stdin.write("q")
                    proc.stdin.flush()
                except Exception:
                    proc.terminate()
    except Exception:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass

    try:
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    err = ""
    try:
        err = (proc.stderr.read() or "")[-800:]
    except Exception:
        pass
    return proc.returncode or 0, err


def remux(src: Path, container: str = "mp4") -> Path:
    """Rewrap the recording without re-encoding."""
    if container == "ts" or not src.exists():
        return src
    exe = str(Path(config.ffmpeg_dir() or "") / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")) \
        if config.ffmpeg_dir() else "ffmpeg"
    dest = src.with_suffix("." + container)
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-c", "copy"]
    if container == "mp4":
        cmd += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
    cmd += [str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=1800,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            src.unlink(missing_ok=True)
            return dest
    except Exception:
        pass
    return src          # keep the .ts rather than lose the recording


# ------------------------------------------------------------------ job body

def run_live(jid: str, url: str, o: dict) -> dict:
    """Worker body for a live recording job."""
    cfg = config.get()
    outdir = Path(o.get("output_dir") or cfg["download_dir"])
    outdir.mkdir(parents=True, exist_ok=True)

    jobs.update(jid, stage="Finding the stream", progress=0.0)
    if o.get("wait_for_live"):
        target = wait_until_live(url, jid, int(o.get("wait_minutes") or 180))
    else:
        target = resolve(url, o.get("quality", "best"), bool(o.get("audio_only")))
        if not target["is_live"] and not o.get("allow_vod"):
            raise RuntimeError(
                "That link is not live right now. Tick \"Wait for it to start\" to have the "
                "app watch for it, or use the Download tab for a normal video.")

    meta = target["meta"]
    jobs.update(jid, title=meta.get("title") or target["title"],
                thumbnail=meta.get("thumbnail", ""))

    stem = _safe_stem(_stamp(meta.get("title") or target["title"]))
    raw = outdir / f"{stem}.ts"
    cmd = build_command(target, raw, o)

    started = time.time()
    limit = float(o.get("max_minutes") or 0) * 60

    def on_tick(stats: dict):
        try:
            secs = int(stats.get("out_time_us", "0") or 0) / 1_000_000
        except ValueError:
            secs = 0.0
        size = int(stats.get("total_size", "0") or 0)
        speed = stats.get("bitrate", "").strip()
        jobs.update(
            jid,
            stage=f"Recording {_hms(secs)}" + (f" of {_hms(limit)}" if limit else ""),
            progress=min(secs / limit, 0.99) if limit else 0.0,
            speed=f"{size / 1048576:.0f} MB" + (f" · {speed}" if speed and speed != "N/A" else ""),
            eta=_hms(max(limit - secs, 0)) if limit else "",
        )

    jobs.update(jid, stage="Recording", progress=0.0)
    code, err = _run_ffmpeg(cmd, jid, on_tick)

    produced = sorted(outdir.glob(f"{glob_escape(stem)} part*.ts")) if o.get("split_minutes") \
        else ([raw] if raw.exists() else [])
    produced = [p for p in produced if p.exists() and p.stat().st_size > 0]

    if not produced:
        raise RuntimeError(
            f"The recording produced no file. ffmpeg said:\n{err.strip() or f'exit code {code}'}")

    jobs.update(jid, stage="Finalising", speed="", eta="")
    container = o.get("container", "mp4")
    finals = [remux(p, container) for p in produced]
    for f in finals:
        jobs.add_file(jid, str(f), "recording")

    # A user-requested stop is a successful recording, not a failed job.
    jobs.clear_cancel(jid)
    elapsed = time.time() - started
    total = sum(f.stat().st_size for f in finals if f.exists())
    return {
        "meta": meta,
        "detail": {"engine": "Live recording (ffmpeg, stream copy)",
                   "duration": round(elapsed), "duration_text": _hms(elapsed),
                   "files": len(finals), "size_mb": round(total / 1048576, 1),
                   "container": container},
        "output_dir": str(outdir),
    }


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]?*])", r"[\1]", text)


def _hms(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


_HAS_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T_]\d{1,2}[:.\-_]\d{2}\s*$")


def _stamp(title: str) -> str:
    """Timestamp the filename so repeat recordings never collide -- unless the
    site already put one in the title, which YouTube live does."""
    title = (title or "Live recording").strip()
    if _HAS_STAMP.search(title):
        return title
    return f"{title} {time.strftime('%Y-%m-%d %H-%M')}"


def _safe_stem(title: str, limit: int = 120) -> str:
    bad = '<>:"/\\|?*'
    out = "".join(("-" if c in bad else c) for c in title).strip(" .")
    return " ".join(out.split())[:limit].strip() or "live recording"
