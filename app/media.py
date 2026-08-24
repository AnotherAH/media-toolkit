"""yt-dlp engine: probing, downloading, and pulling native captions.

Everything site-specific lives in yt-dlp. This module only maps UI choices onto
yt-dlp options and forwards progress into the job registry.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import config, ffmpegtools, jobs, subs

# Height-capped selectors. The trailing /bv*+ba/b keeps a download alive when a
# site has nothing at or under the cap.
VIDEO_PRESETS: dict[str, str] = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",
    "360": "bv*[height<=360]+ba/b[height<=360]/bv*+ba/b",
    # H.264 + AAC in MP4: what phones, TVs and editing software all accept.
    "compatible": "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/bv*+ba/b",
    "smallest": "wv*+wa/w",
}

AUDIO_CODECS = ("mp3", "m4a", "opus", "flac", "wav", "aac", "vorbis")
SPONSOR_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic"]
CAPTION_EXT_PRIORITY = ("json3", "srv3", "vtt", "ttml", "srv1", "srv2")


# --------------------------------------------------------------------- options

def base_opts(quiet: bool = True) -> dict[str, Any]:
    cfg = config.get()
    opts: dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": max(1, int(cfg["concurrent_fragments"])),
        "restrictfilenames": bool(cfg["restrict_filenames"]),
        "windowsfilenames": os.name == "nt",
        "trim_file_name": 180,
        "overwrites": False,
        "continuedl": True,
        "updatetime": bool(cfg["set_mtime"]),
    }
    if (d := config.ffmpeg_dir()):
        opts["ffmpeg_location"] = d

    # --- network ------------------------------------------------------------
    if cfg["proxy"]:
        opts["proxy"] = cfg["proxy"]
    if cfg["rate_limit"]:
        opts["ratelimit"] = _parse_rate(cfg["rate_limit"])
    if cfg["throttled_rate"]:
        opts["throttledratelimit"] = _parse_rate(cfg["throttled_rate"])
    if cfg["force_ipv4"]:
        opts["source_address"] = "0.0.0.0"
    if cfg["geo_bypass_country"]:
        opts["geo_bypass_country"] = cfg["geo_bypass_country"].upper()[:2]
    headers = {}
    if cfg["user_agent"]:
        headers["User-Agent"] = cfg["user_agent"]
    if cfg["referer"]:
        headers["Referer"] = cfg["referer"]
    if headers:
        opts["http_headers"] = headers
    for key, opt in (("sleep_requests", "sleep_interval_requests"),
                     ("sleep_interval", "sleep_interval"),
                     ("max_sleep_interval", "max_sleep_interval")):
        if float(cfg[key] or 0) > 0:
            opts[opt] = float(cfg[key])
    if cfg["external_downloader"]:
        opts["external_downloader"] = {"default": cfg["external_downloader"]}
        if cfg["external_downloader"] == "aria2c":
            opts["external_downloader_args"] = {
                "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "--console-log-level=warn"]}

    # TLS fingerprint impersonation, for sites that block non-browser clients.
    if cfg["impersonate"]:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget.from_str(cfg["impersonate"])
        except Exception:
            pass

    # --- cookies ------------------------------------------------------------
    if cfg["cookies_file"] and Path(cfg["cookies_file"]).exists():
        opts["cookiefile"] = cfg["cookies_file"]
    elif cfg["cookies_browser"]:
        opts["cookiesfrombrowser"] = (
            cfg["cookies_browser"],
            cfg["cookies_profile"] or None,
            None,
            cfg["cookies_container"] or None,
        )
    return opts


def impersonate_targets() -> list[str]:
    """Browser fingerprints curl_cffi can imitate, if it is installed."""
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            targets = ydl._get_available_impersonate_targets()
        return [str(t[0]) for t in targets]
    except Exception:
        return []


def _parse_rate(text: str) -> int | None:
    text = str(text).strip().upper().rstrip("B")
    if not text:
        return None
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}.get(text[-1:])
    try:
        return int(float(text[:-1]) * mult) if mult else int(float(text))
    except ValueError:
        return None


def build_download_opts(o: dict, outdir: str) -> dict[str, Any]:
    """Translate the UI form into yt-dlp options."""
    cfg = config.get()
    opts = base_opts()
    mode = o.get("mode", "video")
    pps: list[dict] = []

    tmpl = o.get("output_template") or cfg["output_template"]
    if o.get("playlist") and o.get("subfolder", True):
        tmpl = "%(playlist_title,playlist_id|Playlist)s/%(playlist_index)03d - " + tmpl
    opts["outtmpl"] = str(Path(outdir) / tmpl)
    opts["noplaylist"] = not o.get("playlist")

    # Stage partial files in temp so the library never shows half-written media.
    # Thumbnail postprocessors resolve their input against the home path, so the
    # split breaks them -- skip staging whenever a thumbnail is in play.
    wants_thumbnail = any(o.get(k, cfg.get(k, False)) for k in
                          ("embed_thumbnail", "write_thumbnail", "convert_thumbnails",
                           "write_all_thumbnails"))
    if cfg["use_temp_dir"] and not wants_thumbnail:
        opts["paths"] = {"home": str(outdir), "temp": temp_dir()}
        opts["outtmpl"] = tmpl

    # ---------------------------------------------------------------- playlist
    if o.get("playlist"):
        opts["ignoreerrors"] = "only_download"
        if o.get("playlist_items"):
            opts["playlist_items"] = str(o["playlist_items"])
        if o.get("archive"):
            opts["download_archive"] = str(Path(outdir) / ".download-archive.txt")
            if o.get("stop_at_known"):
                opts["break_on_existing"] = True
        if o.get("playlist_order") == "reverse":
            opts["playlistreverse"] = True
        elif o.get("playlist_order") == "random":
            opts["playlistrandom"] = True
        if o.get("max_downloads"):
            opts["max_downloads"] = int(o["max_downloads"])
        if o.get("concat_playlist"):
            opts["concat_playlist"] = "always"

    _apply_filters(opts, o)

    # ------------------------------------------------------------------ format
    if mode == "audio":
        codec = o.get("audio_codec", "mp3")
        codec = codec if codec in AUDIO_CODECS else "mp3"
        opts["format"] = "ba/b"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": codec,
                    "preferredquality": str(o.get("audio_quality", "0"))})
    else:
        quality = o.get("quality", "best")
        opts["format"] = VIDEO_PRESETS.get(quality, VIDEO_PRESETS["best"])
        if quality == "compatible":
            # The ordering yt-dlp itself uses for its "-t mp4" preset.
            opts["format_sort"] = ["vcodec:h264", "lang", "quality", "res",
                                   "fps", "hdr:12", "acodec:aac"]
        container = o.get("container", "mp4")
        encoder = o.get("recode_encoder", cfg["recode_encoder"])
        if encoder:
            spec = next((e for e in ffmpegtools.ENCODERS if e["id"] == encoder), None)
            target = (spec or {}).get("container", container)
            opts["merge_output_format"] = target
            # Handled by ForceRecodePP in run_download, because the stock
            # converter no-ops when the container already matches.
            opts["_recode"] = {
                "args": ffmpegtools.recode_args(
                    encoder, o.get("recode_quality", cfg["recode_quality"]),
                    o.get("normalize_audio", cfg["normalize_audio"])),
                "ext": target,
                "vcodec": (spec or {}).get("vcodec", ""),
            }
        elif container in ("mp4", "mkv", "webm"):
            opts["merge_output_format"] = container
            pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": container})

    # --------------------------------------------------------------- subtitles
    want_subs = o.get("subtitles", "none")
    if want_subs != "none" and mode != "audio":
        langs = [s.strip() for s in (o.get("subtitle_langs") or cfg["subtitle_langs"]).split(",") if s.strip()]
        opts["subtitleslangs"] = langs or ["en"]
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = want_subs in ("auto", "both")
        opts["subtitlesformat"] = "srt/best"
        if o.get("embed_subs"):
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
        else:
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})

    # ------------------------------------------------------------ sponsorblock
    if o.get("sponsorblock", cfg["sponsorblock"]):
        cats = o.get("sponsorblock_categories") or ["sponsor", "selfpromo", "interaction"]
        pps.append({"key": "SponsorBlock", "categories": cats, "when": "after_filter"})
        mode_sb = o.get("sponsorblock_mode", cfg["sponsorblock_mode"])
        if mode_sb == "remove":
            pps.append({"key": "ModifyChapters", "remove_sponsor_segments": cats,
                        "force_keyframes": bool(o.get("force_keyframes"))})
        # "mark" leaves the segments in place; SponsorBlock has already written
        # chapter markers, so FFmpegMetadata below bakes them into the file.

    # ------------------------------------------------------------- chapter ops
    if o.get("remove_chapters"):
        patterns = [p.strip() for p in str(o["remove_chapters"]).split(",") if p.strip()]
        if patterns:
            import re as _re
            pps.append({"key": "ModifyChapters",
                        "remove_chapters_patterns": [_re.compile(p) for p in patterns]})

    # Metadata before thumbnail, matching the yt-dlp CLI: running FFmpegMetadata
    # after EmbedThumbnail can strip the cover art back out of some containers.
    if o.get("embed_metadata", cfg["embed_metadata"]):
        pps.append({"key": "FFmpegMetadata",
                    "add_metadata": True,
                    "add_chapters": bool(o.get("embed_chapters", cfg["embed_chapters"]))})

    # Convert before embedding, so the embedded art is the converted one and the
    # converter still has a file to work on.
    convert_thumbs = o.get("convert_thumbnails", cfg["convert_thumbnails"])
    if convert_thumbs:
        opts["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": convert_thumbs})

    if o.get("embed_thumbnail", cfg["embed_thumbnail"]):
        opts["writethumbnail"] = True
        # Keep the image on disk only if it was explicitly asked for; otherwise
        # EmbedThumbnail cleans it up.
        pps.append({"key": "EmbedThumbnail",
                    "already_have_thumbnail": bool(o.get("write_thumbnail")
                                                   or o.get("write_all_thumbnails"))})

    if o.get("split_chapters"):
        pps.append({"key": "FFmpegSplitChapters",
                    "force_keyframes": bool(o.get("force_keyframes"))})

    # ------------------------------------------------------------- extra files
    if o.get("write_info_json", cfg["write_info_json"]):
        opts["writeinfojson"] = True
    if o.get("write_description", cfg["write_description"]):
        opts["writedescription"] = True
    if o.get("write_comments", cfg["write_comments"]):
        opts["getcomments"] = True
        opts["writeinfojson"] = True          # comments live inside the info json
        # Uncapped comment extraction never finishes on a popular video -- yt-dlp
        # will happily page through millions. Take the top slice instead.
        limit = int(o.get("max_comments") or 200)
        opts.setdefault("extractor_args", {}).setdefault("youtube", {}).update({
            "max_comments": [str(limit), "all", "10", "3", "2"],
            "comment_sort": ["top"],
        })
    if o.get("write_thumbnail"):
        opts["writethumbnail"] = True
    if o.get("write_all_thumbnails"):
        opts["write_all_thumbnails"] = True
    if o.get("write_link"):
        opts["writeurllink"] = os.name == "nt"
        opts["writelink"] = os.name != "nt"
    # ---------------------------------------------------------------- sections
    ranges, chapters = _parse_sections(o.get("section", ""))
    if ranges or chapters:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(chapters, ranges)
        opts["force_keyframes_at_cuts"] = bool(o.get("force_keyframes", True))

    # -------------------------------------------------------------------- live
    if o.get("live_from_start"):
        opts["live_from_start"] = True
    if o.get("wait_for_video"):
        opts["wait_for_video"] = (10, 600)

    if pps:
        opts["postprocessors"] = pps
    return opts


def _apply_filters(opts: dict, o: dict) -> None:
    """Duration/views/title/date/size filters -- the useful half of --match-filters."""
    from yt_dlp.utils import DateRange, match_filter_func

    filters: list[str] = []
    if o.get("min_duration"):
        filters.append(f"duration >= {int(float(o['min_duration']) * 60)}")
    if o.get("max_duration"):
        filters.append(f"duration <= {int(float(o['max_duration']) * 60)}")
    if o.get("min_views"):
        filters.append(f"view_count >= {int(o['min_views'])}")
    if o.get("title_contains"):
        safe = str(o["title_contains"]).replace("&", r"\&")
        filters.append(f"title ~= (?i){safe}")
    # Never both ask to record a live stream and filter live streams out.
    if o.get("skip_live") and not (o.get("live_from_start") or o.get("wait_for_video")):
        filters.append("!is_live")
    if filters:
        # A single &-joined filter means every condition must hold; separate
        # entries would be OR-ed, which is not what a filter form implies.
        opts["match_filter"] = match_filter_func(" & ".join(filters))

    after, before = o.get("date_after", ""), o.get("date_before", "")
    if after or before:
        opts["daterange"] = DateRange(after or None, before or None)
    if o.get("min_filesize"):
        opts["min_filesize"] = _parse_rate(o["min_filesize"])
    if o.get("max_filesize"):
        opts["max_filesize"] = _parse_rate(o["max_filesize"])


def _parse_sections(spec: str):
    """Accept '1:30-4:15', '*10:00-inf' and bare chapter names, comma separated.

    Returns (time_ranges, chapter_regexes) for yt-dlp download_range_func.
    """
    from yt_dlp.utils import parse_duration

    ranges: list[tuple[float, float]] = []
    chapters: list[str] = []
    for part in [p.strip() for p in str(spec or "").split(",") if p.strip()]:
        body = part[1:] if part.startswith("*") else part
        left, sep, right = body.partition("-")
        if sep:
            start = parse_duration(left.strip()) if left.strip() else 0
            right = right.strip().lower()
            end = float("inf") if right in ("inf", "end", "") else parse_duration(right)
            if start is not None and end is not None:
                ranges.append((float(start or 0), float(end)))
                continue
        chapters.append(body)
    return ranges, chapters


def list_formats(url: str) -> dict:
    """A real format table, so an exact stream can be picked by hand."""
    opts = base_opts()
    opts.update({"noplaylist": True, "skip_download": True})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    rows = []
    for f in (info.get("formats") or []):
        if f.get("format_id") in (None, "source"):
            continue
        rows.append({
            "format_id": f.get("format_id", ""),
            "ext": f.get("ext", ""),
            "resolution": f.get("resolution") or (
                f"{f.get('width')}x{f.get('height')}" if f.get("height") else "audio only"),
            "height": f.get("height") or 0,
            "fps": f.get("fps") or 0,
            "vcodec": (f.get("vcodec") or "none").split(".")[0],
            "acodec": (f.get("acodec") or "none").split(".")[0],
            "abr": round(f.get("abr") or 0),
            "tbr": round(f.get("tbr") or 0),
            "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
            "proto": f.get("protocol", ""),
            "note": f.get("format_note", ""),
            "dynamic_range": f.get("dynamic_range") or "",
        })
    rows.sort(key=lambda r: (r["height"], r["tbr"]), reverse=True)
    return {"title": info.get("title", ""), "formats": rows}


# ------------------------------------------------------------------- probing

def probe(url: str, flat_playlist: bool = True) -> dict:
    """Metadata for the preview card. Never downloads."""
    opts = base_opts()
    opts.update({"noplaylist": False, "skip_download": True,
                 "extract_flat": "in_playlist" if flat_playlist else False})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return summarize(info)


def summarize(info: dict) -> dict:
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return {
            "kind": "playlist",
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "count": info.get("playlist_count") or len(entries),
            "url": info.get("webpage_url") or "",
            "thumbnail": (entries[0].get("thumbnails") or [{}])[-1].get("url", "") if entries else "",
            "entries": [{"title": e.get("title") or "Untitled",
                         "url": e.get("url") or e.get("webpage_url") or "",
                         "duration": e.get("duration") or 0} for e in entries[:200]],
        }

    heights = sorted({f.get("height") for f in (info.get("formats") or [])
                      if f.get("height")}, reverse=True)
    manual = sorted((info.get("subtitles") or {}).keys())
    auto = sorted((info.get("automatic_captions") or {}).keys())
    return {
        "kind": "video",
        "id": info.get("id", ""),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "duration_string": info.get("duration_string") or _dur(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "url": info.get("webpage_url") or "",
        "extractor": info.get("extractor_key") or "",
        "upload_date": _date(info.get("upload_date")),
        "view_count": info.get("view_count") or 0,
        "is_live": bool(info.get("is_live")),
        "description": (info.get("description") or "")[:600],
        "heights": heights,
        "subtitles": manual,
        "auto_captions": auto,
        "language": info.get("language") or "",
        "has_captions": bool(manual or auto),
        "filesize_approx": info.get("filesize_approx") or 0,
    }


def _dur(seconds: float) -> str:
    s = int(seconds or 0)
    if not s:
        return ""
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _date(raw: str | None) -> str:
    if raw and len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw or ""


def supported_sites(query: str = "", limit: int = 400) -> dict:
    """Searchable list of every extractor yt-dlp ships."""
    from yt_dlp.extractor import list_extractor_classes
    names = []
    for cls in list_extractor_classes():
        name = cls.IE_NAME
        if not name or name.startswith("generic"):
            continue
        names.append(name)
    names = sorted(set(names), key=str.lower)
    hits = [n for n in names if query.lower() in n.lower()] if query else names
    return {"total": len(names), "matches": hits[:limit], "truncated": len(hits) > limit}


# ------------------------------------------------------------ native captions

def _pick_track(info: dict, preferred: list[str]) -> tuple[list[dict], str, str] | None:
    """Choose the best caption track: manual over auto, original over translated."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    own = (info.get("language") or "").split("-")[0]

    def matches(pool: dict, code: str) -> str | None:
        if code in pool:
            return code
        for key in pool:
            if key.split("-")[0].lower() == code.lower():
                return key
        return None

    order: list[tuple[dict, str, str]] = []
    for code in preferred:
        if (k := matches(manual, code)):
            order.append((manual, k, "manual"))
    if own:
        if (k := matches(manual, own)):
            order.append((manual, k, "manual"))
    for code in preferred:
        # YouTube exposes the true machine transcript as "<lang>-orig"; the plain
        # code may be a machine translation of it, which reads noticeably worse.
        for candidate in (f"{code}-orig", code):
            if (k := matches(auto, candidate)):
                order.append((auto, k, "auto"))
                break
    if own:
        for candidate in (f"{own}-orig", own):
            if (k := matches(auto, candidate)):
                order.append((auto, k, "auto"))
                break
    if manual:
        first = sorted(manual)[0]
        order.append((manual, first, "manual"))

    for pool, key, source in order:
        tracks = pool.get(key) or []
        if tracks:
            return tracks, key, source
    return None


def fetch_captions(url: str, preferred: list[str] | None = None,
                   info: dict | None = None) -> tuple[list[subs.Segment], dict, str] | None:
    """Grab captions the site already has. Returns (segments, meta, label) or None."""
    preferred = preferred or ["en"]
    opts = base_opts()
    opts.update({"skip_download": True, "noplaylist": True,
                 "writesubtitles": True, "writeautomaticsub": True})
    with YoutubeDL(opts) as ydl:
        if info is None:
            info = ydl.extract_info(url, download=False)
        chosen = _pick_track(info, preferred)
        if not chosen:
            return None
        tracks, lang, source = chosen
        ranked = sorted(
            tracks,
            key=lambda t: CAPTION_EXT_PRIORITY.index(t.get("ext", ""))
            if t.get("ext") in CAPTION_EXT_PRIORITY else 99,
        )
        for track in ranked:
            try:
                raw = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
            except Exception:
                continue
            segments = subs.parse_auto(raw, track.get("ext", ""))
            if segments:
                label = f"{'Official' if source == 'manual' else 'Auto-generated'} captions ({lang})"
                return segments, summarize(info), label
    return None


def download_audio(url: str, outdir: str, hook=None) -> tuple[str, dict]:
    """Fetch the smallest usable audio stream for speech recognition."""
    opts = base_opts()
    opts.update({
        "format": "ba[abr<=128]/ba/b",
        "outtmpl": str(Path(outdir) / "%(id)s.%(ext)s"),
        "noplaylist": True,
    })
    if hook:
        opts["progress_hooks"] = [hook]
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not Path(path).exists():
        stem = Path(path).with_suffix("")
        hits = list(Path(outdir).glob(stem.name + ".*"))
        if not hits:
            raise RuntimeError("Audio download produced no file")
        path = str(hits[0])
    return path, summarize(info)


# ------------------------------------------------------------------ download

def run_download(jid: str, url: str, options: dict) -> dict:
    """Worker body for a download job."""
    cfg = config.get()
    outdir = options.get("output_dir") or cfg["download_dir"]
    Path(outdir).mkdir(parents=True, exist_ok=True)
    opts = build_download_opts(options, outdir)
    produced: list[str] = []

    # Extraction happens before any progress hook fires, so say what is going on.
    jobs.update(jid, stage="Fetching comments" if opts.get("getcomments") else "Reading the page")

    def progress(d: dict):
        jobs.raise_if_cancelled(jid)
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            info = d.get("info_dict") or {}
            n, count = info.get("playlist_index"), info.get("n_entries")
            stage = "Downloading"
            if n and count:
                frac = ((n - 1) + frac) / count
                stage = f"Downloading {n}/{count}"
            jobs.update(jid, progress=min(frac, 0.99), stage=stage,
                        speed=_fmt_speed(d.get("speed")),
                        eta=_fmt_eta(d.get("eta")))
        elif d.get("status") == "finished":
            if d.get("filename"):
                produced.append(d["filename"])
            jobs.update(jid, stage="Processing with ffmpeg", speed="", eta="")

    def pp_hook(d: dict):
        jobs.raise_if_cancelled(jid)
        if d.get("status") == "started":
            jobs.update(jid, stage=f"{d.get('postprocessor', 'Processing')}")
        elif d.get("status") == "finished":
            info = d.get("info_dict") or {}
            for key in ("filepath", "__finaldir"):
                if info.get(key) and Path(str(info[key])).is_file():
                    produced.append(str(info[key]))

    opts["progress_hooks"] = [progress]
    opts["postprocessor_hooks"] = [pp_hook]
    recode = opts.pop("_recode", None)

    with YoutubeDL(opts) as ydl:
        if recode:
            from .recode import ForceRecodePP
            pp = ForceRecodePP(ydl, args=recode["args"], ext=recode["ext"],
                               target_vcodec=recode["vcodec"])
            # Must run before metadata and cover art are written, or the
            # re-encode discards them. add_post_processor only appends, so put
            # it at the front of the queue directly.
            try:
                ydl._pps["post_process"].insert(0, pp)
            except Exception:
                ydl.add_post_processor(pp, when="post_process")
        try:
            info = ydl.extract_info(url, download=True)
        except DownloadError as exc:
            raise RuntimeError(str(exc)) from exc

    if info and info.get("_type") != "playlist":
        meta = summarize(info)
        jobs.update(jid, title=meta["title"], thumbnail=meta["thumbnail"])
        final = info.get("requested_downloads") or []
        for entry in final:
            if entry.get("filepath"):
                produced.append(entry["filepath"])
    else:
        meta = summarize(info) if info else {}
        for entry in (info.get("entries") or []) if info else []:
            for got in (entry or {}).get("requested_downloads", []) or []:
                if got.get("filepath"):
                    produced.append(got["filepath"])

    seen: set[str] = set()
    for path in produced:
        p = Path(path)
        if p.suffix.lower() in (".part", ".ytdl", ".webp", ".jpg", ".png"):
            continue
        if p.exists() and str(p) not in seen:
            seen.add(str(p))
            jobs.add_file(jid, str(p), "media")
    return {"meta": meta, "output_dir": outdir, "count": len(seen)}


def _fmt_speed(speed) -> str:
    if not speed:
        return ""
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    val, i = float(speed), 0
    while val >= 1024 and i < 3:
        val, i = val / 1024, i + 1
    return f"{val:.1f} {units[i]}"


def _fmt_eta(eta) -> str:
    if not eta:
        return ""
    eta = int(eta)
    return f"{eta // 60}m {eta % 60}s" if eta >= 60 else f"{eta}s"


def temp_dir() -> str:
    d = Path(tempfile.gettempdir()) / "media-toolkit"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)
