"""yt-dlp engine: probing, format tables and downloads.

Everything site-specific lives in yt-dlp. This module maps the Download tab's
choices onto yt-dlp options, adds the few postprocessors yt-dlp lacks
(app/recode.py), and turns yt-dlp's hooks into plain-language job progress.

Three rules shape it:

* A request never chooses where files go. The folder and the file-name
  pattern come from config only; ``output_dir`` and ``output_template`` in
  job options are ignored.
* Nothing ends as "Done" with no file and no reason. Filtered, archived and
  failed items are counted, and a run that saved nothing says why (Skipped)
  or fails with the real error.
* yt-dlp is asked once. A link is read with ``process=False`` first, so the
  engine knows whether it is a playlist before choosing folders and error
  handling, and then the same result is processed without a second request.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import (ExistingVideoReached, MaxDownloadsReached, ReExtractInfo,
                          download_range_func)

from . import config, errors, ffmpegtools, jobs

# Quality presets the Download tab offers. The cap is applied with yt-dlp's
# "res" sort field, which is the smaller side of the picture, so a 1080x1920
# vertical video counts as 1080p instead of being downgraded to 608x1080.
VIDEO_PRESETS: dict[str, str] = {
    "best": "Best available",
    "2160": "4K",
    "1440": "1440p",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "smallest": "Smallest file",
    "compatible": "Best, plays on any device (older name)",
}

AUDIO_CODECS = ("mp3", "m4a", "opus", "flac", "wav", "aac", "vorbis")
_AUDIO_EXT = {"mp3": "mp3", "m4a": "m4a", "aac": "m4a", "opus": "opus", "flac": "flac",
              "wav": "wav", "vorbis": "ogg"}
CONTAINERS = ("mp4", "mkv", "webm")

# SponsorBlock categories that can be cut or marked, with plain names.
SPONSOR_LABELS: dict[str, str] = {
    "sponsor": "Sponsors",
    "selfpromo": "Self-promotion",
    "interaction": "Like and subscribe reminders",
    "intro": "Intros",
    "outro": "End cards and credits",
    "preview": "Previews and recaps",
    "hook": "Hooks and greetings",
    "filler": "Off-topic tangents",
    "music_offtopic": "Non-music parts of music videos",
}
SPONSOR_CATEGORIES = list(SPONSOR_LABELS)
DEFAULT_SPONSOR = ["sponsor", "selfpromo", "interaction"]

# Windows paths stop working at 260 characters unless long paths are enabled,
# which they are not by default. Titles are shortened to fit, never the id.
PATH_LIMIT = 259
# yt-dlp's intermediate names add up to this much: ".f399-1.webm.part-Frag12.part".
_PARTIAL_SLACK = 30
# " [id]" plus a clip range or variant tag such as " (0.10-0.14)".
_NAME_SLACK = 30
# A playlist's folder name (capped at 50) and the "NNN - " before each item.
_FOLDER_SLACK = 58

_UPDATE_EVERY = 0.25            # seconds between progress updates per job
_DOWNLOAD_SHARE = 0.97          # the rest of the bar is post-processing


# ------------------------------------------------------------------ cookies

# yt-dlp reads its cookie file on first use and writes the whole jar back on
# close, without locking. Several YoutubeDL instances run at once (previews,
# three workers, transcripts, live checks), so a shared file gets truncated or
# reverted. Every instance therefore gets a private copy it may scribble on;
# the user's own file is only ever read.
_COOKIE_DIR = Path(tempfile.gettempdir()) / "media-toolkit" / "cookies"
_COOKIE_MAX_AGE = 6 * 3600
_cookie_lock = threading.Lock()
_cookie_copies: set[str] = set()
_cookie_pruned = 0.0


def _prune_cookie_copies() -> None:
    global _cookie_pruned
    now = time.time()
    if now - _cookie_pruned < 60:
        return
    _cookie_pruned = now
    try:
        for p in _COOKIE_DIR.glob("*.txt"):
            with contextlib.suppress(OSError):
                if now - p.stat().st_mtime > _COOKIE_MAX_AGE:
                    p.unlink()
    except OSError:
        pass


def _private_cookie_copy(src: str) -> str | None:
    """A fresh copy of the user's cookies.txt for one YoutubeDL instance."""
    try:
        _COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        dest = _COOKIE_DIR / f"{uuid.uuid4().hex}.txt"
        shutil.copyfile(src, dest)
    except OSError:
        return None
    with _cookie_lock:
        _cookie_copies.add(str(dest))
        _prune_cookie_copies()
    return str(dest)


def release(opts: dict) -> None:
    """Delete the private cookie copy base_opts made for these options."""
    path = (opts or {}).get("cookiefile")
    if not isinstance(path, str):
        return
    with _cookie_lock:
        if path not in _cookie_copies:
            return
        _cookie_copies.discard(path)
    with contextlib.suppress(OSError):
        os.unlink(path)


@atexit.register
def _drop_cookie_copies() -> None:
    with _cookie_lock:
        paths = list(_cookie_copies)
        _cookie_copies.clear()
    for path in paths:
        with contextlib.suppress(OSError):
            os.unlink(path)


@contextlib.contextmanager
def session(opts: dict):
    """YoutubeDL for these options; its private cookie copy is removed after."""
    try:
        with YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        release(opts)


# -------------------------------------------------------------- JS runtimes

# yt-dlp key -> executable names. YouTube needs a JavaScript runtime to solve
# its player challenges; yt-dlp only enables deno unless told otherwise, so an
# installed Node.js was detected and advertised but never used.
_JS_RUNTIMES = (("deno", ("deno",)), ("node", ("node",)), ("bun", ("bun",)),
                ("quickjs", ("qjs", "quickjs")))
_js_cache: tuple[float, dict] = (0.0, {})


def js_runtimes() -> dict[str, dict]:
    """{yt-dlp runtime key: {"path": exe}} for every runtime on this PC,
    looking in the app's bin folders first. Cached for a minute, so a runtime
    installed while the app runs is picked up without a restart."""
    global _js_cache
    stamp, found = _js_cache
    if time.time() - stamp < 60:
        return dict(found)
    folders = [config.BIN_DIR, config.RUNTIME_DIR / "bin"]
    found = {}
    for key, names in _JS_RUNTIMES:
        for name in names:
            exe = next((str(d / f"{name}.exe") for d in folders
                        if (d / f"{name}.exe").is_file()), None) or shutil.which(name)
            if exe:
                found[key] = {"path": exe}
                break
    _js_cache = (time.time(), found)
    return dict(found)


def js_runtime_name() -> str:
    """The runtime yt-dlp will use first ("" when there is none)."""
    return next(iter(js_runtimes()), "")


# ------------------------------------------------------------ impersonation

_imp_cache: list | None = None


def _available_targets() -> list:
    global _imp_cache
    if _imp_cache is None:
        try:
            with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                _imp_cache = [t for t, _ in ydl._get_available_impersonate_targets()]
        except Exception:
            _imp_cache = []
    return _imp_cache


def impersonate_targets() -> list[str]:
    """Browser families curl_cffi can imitate here ("chrome", "edge", ...).

    Settings offers families, not the dozens of exact browser versions:
    yt-dlp picks the newest version of a family by itself.
    """
    return sorted({str(t.client) for t in _available_targets() if getattr(t, "client", None)})


def _impersonate(value: str):
    """ImpersonateTarget for a family ("chrome") or exact target
    ("chrome-131:windows-10"), or None when this PC cannot do it. yt-dlp
    refuses to start at all with an unavailable target, so never pass one."""
    value = (value or "").strip().lower()
    if not value:
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        target = ImpersonateTarget.from_str(value)
    except Exception:
        return None
    if any(target in t for t in _available_targets()):
        return target
    return None


# -------------------------------------------------------------------- rates

_RATE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([kmgt]?)\s*(i?b(?:ps|/s)?|bit/s|ps|/s)?$", re.I)


def _parse_rate(text, bare: str = "M") -> int | None:
    """Bytes per second from '5M', '5 MB/s', '500K', '1.5 MiB/s', or a bare
    number in the unit the field shows (MB/s for the speed limit). Returns
    None for anything else, so a typo never becomes a 5 bytes/s limit."""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _RATE.match(raw)
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "").upper()
    tail = (m.group(3) or "").lower()
    if not unit and not tail:
        unit = bare.upper()
    value = number * (1024 ** "BKMGT".index(unit or "B"))
    if tail.endswith("bps") and not tail.startswith("b"):
        pass                                   # "5 MBps" is bytes
    elif tail in ("bps", "bit/s") and m.group(3) and m.group(3)[0] == "b":
        value /= 8                             # "5 Mbps", lower-case b: bits
    return int(value) if value >= 1 else None


# --------------------------------------------------------------------- base

def base_opts(quiet: bool = True) -> dict[str, Any]:
    """Options every YoutubeDL in the app shares: network, sign-in, ffmpeg.

    Safe for concurrent use: the cookie file, if any, is a private copy for
    this call (see release()/session()).
    """
    cfg = config.get()
    try:
        fragments = max(1, min(16, int(float(cfg.get("concurrent_fragments") or 1))))
    except (TypeError, ValueError):
        fragments = 1
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
        "concurrent_fragment_downloads": fragments,
        "restrictfilenames": bool(cfg.get("restrict_filenames")),
        "windowsfilenames": os.name == "nt",
        "trim_file_name": 180,
        "overwrites": False,
        "continuedl": True,
        "updatetime": bool(cfg.get("set_mtime")),
    }
    if (d := config.ffmpeg_dir()):
        opts["ffmpeg_location"] = d
    runtimes = js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes

    # --- network ------------------------------------------------------------
    if cfg.get("proxy"):
        opts["proxy"] = cfg["proxy"]
    if (rate := _parse_rate(cfg.get("rate_limit"), bare="M")):
        opts["ratelimit"] = rate
    if (rate := _parse_rate(cfg.get("throttled_rate"), bare="K")):
        opts["throttledratelimit"] = rate
    if cfg.get("force_ipv4"):
        opts["source_address"] = "0.0.0.0"
    country = str(cfg.get("geo_bypass_country") or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", country):
        opts["geo_bypass_country"] = country.upper()
    headers = {}
    if cfg.get("user_agent"):
        headers["User-Agent"] = cfg["user_agent"]
    if cfg.get("referer"):
        headers["Referer"] = cfg["referer"]
    if headers:
        opts["http_headers"] = headers
    for key, opt in (("sleep_requests", "sleep_interval_requests"),
                     ("sleep_interval", "sleep_interval"),
                     ("max_sleep_interval", "max_sleep_interval")):
        try:
            value = float(cfg.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            opts[opt] = value
    # Only aria2c, and only when it really is installed: a path here is a
    # program yt-dlp runs.
    if cfg.get("external_downloader") == "aria2c" and shutil.which("aria2c"):
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "--console-log-level=warn"]}

    # TLS fingerprint impersonation, for sites that block non-browser clients.
    target = _impersonate(cfg.get("impersonate") or "")
    if target is not None:
        opts["impersonate"] = target

    # --- cookies ------------------------------------------------------------
    jar = str(cfg.get("cookies_file") or "")
    if jar and Path(jar).is_file():
        copy = _private_cookie_copy(jar)
        if copy:
            opts["cookiefile"] = copy
    elif cfg.get("cookies_browser"):
        opts["cookiesfrombrowser"] = (
            cfg["cookies_browser"],
            cfg.get("cookies_profile") or None,
            None,
            cfg.get("cookies_container") or None,
        )
    return opts


# ------------------------------------------------------------- the request

def _get(o: dict, key: str, cfg: dict, cfg_key: str | None = None, default=None):
    """The job's value, else the remembered Download-tab choice, else default."""
    if key in o and o[key] is not None:
        return o[key]
    return cfg.get(cfg_key or key, default)


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def playlist_mode(o: dict) -> str:
    """'video' | 'all' | 'first' | 'items', including the 1.1 'playlist' flag."""
    mode = str(o.get("playlist_mode") or "").lower()
    if mode in ("video", "all", "first", "items"):
        return mode
    return "all" if _flag(o.get("playlist")) else "video"


_ITEMS = re.compile(r"^\s*-?\d*(?:\s*[-:]\s*-?\d*(?:\s*:\s*-?\d+)?)?\s*(?:,\s*-?\d*(?:\s*[-:]\s*-?\d*(?:\s*:\s*-?\d+)?)?\s*)*$")


def _playlist_items(o: dict, mode: str) -> str | None:
    if mode == "first":
        try:
            n = int(float(o.get("playlist_first") or 10))
        except (TypeError, ValueError):
            n = 10
        return f"1:{max(1, n)}"
    if mode == "items":
        text = str(o.get("playlist_items") or "").strip()
        if not text:
            return None
        if not _ITEMS.match(text):
            raise ValueError(f"“{text}” isn't a list of items. Use numbers and ranges "
                             "such as 1-5, 8.")
        return re.sub(r"\s+", "", text)
    if o.get("playlist_items") and _ITEMS.match(str(o["playlist_items"])):
        return re.sub(r"\s+", "", str(o["playlist_items"]))       # 1.1 requests
    return None


def format_choice(quality: str, compatible: bool, container: str) -> tuple[str, list[str]]:
    """(format, format_sort) for the video quality presets."""
    quality = str(quality or "best").lower()
    if quality == "compatible":                      # the 1.1 preset name
        quality, compatible = "best", True
    fmt = "bv*+ba/b"
    if quality in ("smallest", "worst"):
        # Never "worst": it also reverses the language preference and picks a
        # dubbed audio track in another language.
        sort = ["lang", "+size", "+br", "+res", "+fps"]
        return fmt, (["vcodec:h264"] + sort + ["acodec:aac"]) if compatible else sort
    cap = int(quality) if quality.isdigit() else None
    res = f"res:{cap}" if cap else "res"
    if compatible:
        # yt-dlp's own "-t mp4" order, with the cap ahead of "quality" so a
        # site's own quality ranking cannot outrank the cap.
        return fmt, ["vcodec:h264", "lang", res, "quality", "fps", "hdr:12", "acodec:aac"]
    if container == "mp4":
        # MP4 without the H.264 promise: best picture, but AAC sound where the
        # site has it, which every MP4 player understands.
        return fmt, ["lang", res, "quality", "fps", "hdr:12", "vcodec", "channels", "acodec:aac"]
    return fmt, ([res] if cap else [])


_LANG_CODE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def subtitle_request(o: dict, cfg: dict) -> tuple[list[str], bool] | None:
    """(subtitleslangs, want automatic captions) or None for no subtitles."""
    choice = str(_get(o, "subtitles", cfg, "dl_subtitles", "none") or "none").lower()
    auto = _flag(_get(o, "auto_subs", cfg, "dl_auto_subs", False))
    if choice in ("auto", "both"):                   # 1.1 values
        choice, auto = "custom", True
    elif choice == "manual":
        choice = "custom"
    if choice in ("none", "", "off", "false"):
        return None
    if choice == "all":
        # Every subtitle the uploader made. Automatic captions are left out
        # here: YouTube lists 150-odd machine translations beside them, and
        # fetching them all gets the download rate-limited.
        return ["all", "-live_chat"], False
    if choice == "en":
        codes = ["en"]
    else:
        text = str(_get(o, "subtitle_langs", cfg, "dl_subtitle_langs", "") or
                   cfg.get("subtitle_langs") or "en")
        codes = [c.strip() for c in re.split(r"[,\s]+", text) if c.strip()]
        codes = [c for c in codes if _LANG_CODE.match(c)] or ["en"]
    return [f"{re.escape(c)}(?:-.*)?" for c in codes], auto


def _minutes(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _chapter_pattern(text: str) -> re.Pattern:
    """Plain text, case-insensitive, whole words: 'intro' matches 'Intro' and
    'The intro', never 'Introduction'. Chapter removal deletes content, so a
    loose match would cut parts nobody asked to lose."""
    return re.compile(r"(?<!\w)" + re.escape(text.strip()) + r"(?!\w)", re.IGNORECASE)


def parse_sections(spec) -> tuple[list[tuple[float, float]], list[str]]:
    """'1:30-4:15', '*10:00-inf', '0:05-0:08, 1:00-1:10', a bare start time
    ('1:30' = to the end) or chapter names, comma separated.

    Returns (time ranges, chapter names).
    """
    from yt_dlp.utils import parse_duration

    ranges: list[tuple[float, float]] = []
    chapters: list[str] = []
    for part in [p.strip() for p in str(spec or "").split(",") if p.strip()]:
        body = part[1:].strip() if part.startswith("*") else part
        body = body.replace("–", "-").replace("—", "-")
        left, sep, right = body.partition("-")
        if sep:
            start = parse_duration(left.strip()) if left.strip() else 0
            right = right.strip().lower()
            end = float("inf") if right in ("inf", "end", "") else parse_duration(right)
            if start is not None and end is not None:
                ranges.append((float(start or 0), float(end)))
                continue
        elif re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", body):
            start = parse_duration(body)
            if start is not None:
                ranges.append((float(start), float("inf")))
                continue
        chapters.append(body)
    return ranges, chapters


_parse_sections = parse_sections          # older name


class _Ranges(download_range_func):
    """download_range_func that matches chapter names as plain text and
    remembers the names that matched nothing, so the job can say so."""

    def __init__(self, names: list[str], ranges: list[tuple[float, float]], missed: list[str]):
        super().__init__([_chapter_pattern(n) for n in names], ranges)
        self.names = names
        self.missed = missed

    def __call__(self, info_dict, ydl):
        chapters = info_dict.get("chapters") or []
        for name, pattern in zip(self.names, self.chapters):
            if not any(pattern.search(c.get("title") or "") for c in chapters):
                if name not in self.missed:
                    self.missed.append(name)
        yield from super().__call__(info_dict, ydl)

    def __eq__(self, other):
        return isinstance(other, _Ranges) and super().__eq__(other)

    __hash__ = None


def _date(raw: str | None) -> str:
    if raw and len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw or ""


def normalize_date(text) -> str:
    """YYYYMMDD from '2024-01-01', '2024/01/01', '20240101' or a relative
    date yt-dlp understands ('today-1week'). Raises ValueError otherwise."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8 and re.fullmatch(r"\d{4}\D?\d{2}\D?\d{2}", raw):
        try:
            time.strptime(digits, "%Y%m%d")
            return digits
        except ValueError:
            pass
    try:
        from yt_dlp.utils import date_from_str
        return date_from_str(raw).strftime("%Y%m%d")
    except Exception:
        raise ValueError(f"“{raw}” isn't a date. Use the form 2024-01-31.") from None


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _human_date(ymd: str) -> str:
    return f"{int(ymd[6:])} {_MONTHS[int(ymd[4:6]) - 1]} {ymd[:4]}"


def _human_minutes(v: float) -> str:
    return f"{int(v)}-minute" if v == int(v) else f"{v:g}-minute"


class Filters:
    """The playlist filters, checked in Python with plain-language reasons.

    yt-dlp's match-filter language treated 'Title contains' as a regular
    expression ('C++' matched every C, '(draft' failed the job) and its
    rejection messages are developer text. Each check here only runs when the
    field is known, so flat playlist entries are judged as early as possible
    and the rest after the full page is read.
    """

    def __init__(self, o: dict):
        self.min_s = (_minutes(o.get("min_duration")) or 0) * 60 or None
        self.max_s = (_minutes(o.get("max_duration")) or 0) * 60 or None
        self._min_m, self._max_m = _minutes(o.get("min_duration")), _minutes(o.get("max_duration"))
        try:
            self.min_views = int(float(o.get("min_views") or 0)) or None
        except (TypeError, ValueError):
            self.min_views = None
        self.title = str(o.get("title_contains") or "").strip()
        self._title_cf = self.title.casefold()
        self.after = normalize_date(o.get("date_after"))
        self.before = normalize_date(o.get("date_before"))
        self.max_bytes = None
        self._max_label = ""
        mb = _minutes(o.get("max_filesize_mb"))
        if mb:
            self.max_bytes, self._max_label = int(mb * 1024 * 1024), f"{mb:g} MB"
        elif o.get("max_filesize"):
            size = _parse_rate(o["max_filesize"], bare="M")
            if size:
                self.max_bytes = size
                self._max_label = f"{size / 1024 / 1024:g} MB"

    @property
    def active(self) -> int:
        return sum(bool(x) for x in (self.min_s, self.max_s, self.min_views, self.title,
                                     self.after, self.before, self.max_bytes))

    def check(self, info: dict, incomplete=False) -> str | None:
        """None when the item may be downloaded, else the reason it is skipped."""
        status = info.get("live_status")
        if info.get("is_live") or status == "is_live":
            return "a live stream"
        if status == "is_upcoming":
            return "a live stream that hasn't started yet"
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            if self.min_s and duration < self.min_s:
                return f"shorter than your {_human_minutes(self._min_m)} minimum"
            if self.max_s and duration > self.max_s:
                return f"longer than your {_human_minutes(self._max_m)} limit"
        views = info.get("view_count")
        if self.min_views and isinstance(views, int) and views < self.min_views:
            return f"fewer than {self.min_views:,} views"
        title = info.get("title")
        if self.title and isinstance(title, str) and title and self._title_cf not in title.casefold():
            return f"the title doesn't include “{self.title}”"
        date = str(info.get("upload_date") or "")
        if len(date) == 8 and date.isdigit():
            if self.after and date < self.after:
                return f"uploaded before {_human_date(self.after)}"
            if self.before and date > self.before:
                return f"uploaded after {_human_date(self.before)}"
        if self.max_bytes and incomplete is False:
            size = _selected_size(info)
            if size and size > self.max_bytes:
                return f"larger than your {self._max_label} limit"
        return None


def _selected_size(info: dict) -> int:
    """Bytes of what yt-dlp chose to download (0 when unknown)."""
    parts = info.get("requested_formats") or [info]
    total = 0
    for f in parts:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size:
            return 0
        total += int(size)
    return total


# --------------------------------------------------------- file names

_TITLE_FIELD = re.compile(r"%\(title\)(?:\.(\d+)([Bs])|s)")


def _fit(template: str, budget: int) -> str:
    """Cap every plain %(title) field at ``budget`` characters.

    yt-dlp's trim_file_name cuts the end of the whole name, which removes the
    [id] and lets two long titles map to one file. Shortening the title keeps
    the id. Characters, not bytes: Windows counts UTF-16 characters, so a
    Persian title may keep as many letters as an English one.
    """
    def repl(m: re.Match) -> str:
        current = int(m.group(1)) if m.group(1) else 10 ** 6
        return f"%(title).{max(20, min(current, budget))}s"
    return _TITLE_FIELD.sub(repl, template)


def _with_suffix(template: str) -> str:
    """Add the per-request part (clip range, variant) right before the extension."""
    marker = "%(mt_suffix|)s"
    if marker in template:
        return template
    if template.endswith(".%(ext)s"):
        return template[: -len(".%(ext)s")] + marker + ".%(ext)s"
    return template + marker


def file_templates(cfg: dict, home: str, temp: str | None) -> dict[str, dict[str, str]]:
    """outtmpl for a single video and for a playlist, relative to home.

    Returns {"single": {...}, "playlist": {...}}.
    """
    base = str(cfg.get("output_template") or "")
    if not config.output_template_ok(base):
        base = config.FILENAME_PRESETS["title_id"]
    # Room for the title: the path limit, minus the deepest folder the file
    # passes through, yt-dlp's partial-file endings, and the rest of the name
    # (" [id]", a clip range or variant tag).
    folder = max(len(str(home)), len(str(temp or ""))) + 1
    room = PATH_LIMIT - folder - _PARTIAL_SLACK
    single_title = max(40, room - _NAME_SLACK)
    list_title = max(30, room - _NAME_SLACK - _FOLDER_SLACK)
    folder_part = "%(playlist_title,playlist_id|Playlist).50s"
    chapter = "%(title).{n}s - %(section_number)03d %(section_title).60s [%(id)s].%(ext)s"
    return {
        "single": {
            "default": _with_suffix(_fit(base, single_title)),
            "chapter": chapter.format(n=max(20, single_title - 70)),
            "pl_thumbnail": "",
        },
        "playlist": {
            "default": f"{folder_part}/%(playlist_index)03d - " + _with_suffix(_fit(base, list_title)),
            "chapter": f"{folder_part}/%(playlist_index)03d - " + chapter.format(n=max(20, list_title - 70)),
            "pl_thumbnail": "",
            "pl_description": f"{folder_part}/{folder_part} [%(playlist_id)s].%(ext)s",
            "pl_infojson": f"{folder_part}/{folder_part} [%(playlist_id)s].%(ext)s",
            "pl_video": "%(playlist_title,title,playlist_id|Playlist).100s [%(playlist_id,id)s].%(ext)s",
        },
        "trim": max(60, room),
    }


# ----------------------------------------------------------- the options

def build_download_opts(o: dict, outdir: str, temp: str | None = None) -> dict[str, Any]:
    """Translate a download request into yt-dlp options.

    Everything is decided here except what depends on the link itself (is it
    a playlist?). That part is in opts["_playlist"], applied by run_download
    once the link has been read. opts["postprocessors"] lists yt-dlp's own
    postprocessors by key and the app's ones with an "MT" prefix, in the order
    they run; run_download builds them.
    """
    o = dict(o or {})
    for key in ("output_dir", "output_template"):   # never from a request
        o.pop(key, None)
    cfg = config.get()
    opts = base_opts()
    mode = "audio" if str(_get(o, "mode", cfg, "dl_mode", "video")).lower() == "audio" else "video"
    pl_mode = playlist_mode(o)
    notes: list[str] = []

    # ---------------------------------------------------------------- files
    if cfg.get("use_temp_dir", True) and temp:
        opts["paths"] = {"home": str(outdir), "temp": str(temp)}
    else:
        opts["paths"] = {"home": str(outdir)}
    names = file_templates(cfg, str(outdir), temp if "temp" in opts["paths"] else None)
    opts["outtmpl"] = dict(names["single"])
    opts["trim_file_name"] = names["trim"]
    opts["noplaylist"] = pl_mode == "video"

    # -------------------------------------------------------------- playlist
    playlist: dict[str, Any] = {"outtmpl": dict(names["playlist"]),
                                "ignoreerrors": "only_download"}
    items = _playlist_items(o, pl_mode)
    if items:
        opts["playlist_items"] = items
    if _flag(o.get("archive")):
        opts["download_archive"] = str(Path(outdir) / ".download-archive.txt")
        if _flag(o.get("stop_at_known")):
            opts["break_on_existing"] = True
    order = str(o.get("playlist_order") or "").lower()
    if order == "reverse":
        opts["playlistreverse"] = True
    elif order in ("random", "shuffle"):
        opts["playlistrandom"] = True
    try:
        if int(o.get("max_downloads") or 0) > 0:
            opts["max_downloads"] = int(o["max_downloads"])
    except (TypeError, ValueError):
        pass
    concat = _flag(o.get("concat_playlist"))

    write_thumb = _flag(_get(o, "write_thumbnail", cfg, "dl_write_thumbnail", False))
    write_json = _flag(_get(o, "write_info_json", cfg, "write_info_json", False))
    opts["allow_playlist_files"] = bool(write_json or write_thumb)
    if write_thumb:
        playlist["outtmpl"]["pl_thumbnail"] = playlist["outtmpl"]["pl_infojson"]

    # --------------------------------------------------------------- filters
    filters = Filters(o)
    opts["_filters"] = filters
    if filters.max_bytes:
        opts["max_filesize"] = filters.max_bytes     # also stops plain HTTP downloads early
    try:
        if _parse_rate(o.get("min_filesize"), bare="M"):
            opts["min_filesize"] = _parse_rate(o["min_filesize"], bare="M")
    except (TypeError, ValueError):
        pass

    # ---------------------------------------------------------------- format
    pps: list[dict] = []
    cats: list[str] = []
    sb_mode = "remove"
    sponsor = _flag(_get(o, "sponsorblock", cfg, "sponsorblock", False))
    if sponsor:
        cats = o.get("sponsorblock_categories") or cfg.get("sponsorblock_categories") or DEFAULT_SPONSOR
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",")]
        cats = [c for c in cats if c in SPONSOR_LABELS] or list(DEFAULT_SPONSOR)
        sb_mode = str(_get(o, "sponsorblock_mode", cfg, "sponsorblock_mode", "remove")).lower()
        sb_mode = sb_mode if sb_mode in ("remove", "mark") else "remove"
        pps.append({"key": "SponsorBlock", "categories": cats, "when": "after_filter"})
    embed_meta = _flag(_get(o, "embed_metadata", cfg, "embed_metadata", True))
    embed_chapters = _flag(_get(o, "embed_chapters", cfg, "embed_chapters", True))
    normalize = _flag(_get(o, "normalize_audio", cfg, "normalize_audio", False))
    container = str(_get(o, "container", cfg, "dl_container", "mp4") or "mp4").lower()
    container = container if container in CONTAINERS else "mp4"
    final_ext = None
    format_id = str(o.get("format_id") or "").strip()
    if format_id and not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", format_id):
        format_id = ""

    convert_thumbs = str(_get(o, "convert_thumbnails", cfg, "convert_thumbnails", "") or "").lower()
    convert_thumbs = convert_thumbs if convert_thumbs in ("jpg", "png") else ""
    if convert_thumbs:
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": convert_thumbs,
                    "when": "before_dl"})

    if mode == "audio":
        codec = str(_get(o, "audio_codec", cfg, "dl_audio_codec", "mp3") or "mp3").lower()
        codec = codec if codec in AUDIO_CODECS else "mp3"
        codec = "m4a" if codec == "aac" else codec
        final_ext = _AUDIO_EXT[codec]
        opts["format"] = format_id or "ba/b"
        prefer = {"m4a": "aac", "opus": "opus", "vorbis": "vorbis"}.get(codec)
        if prefer and not format_id:
            # A matching source is copied instead of re-encoded.
            opts["format_sort"] = ["lang", f"acodec:{prefer}"]
        pps.append({"key": "MTExtractAudio" if normalize else "FFmpegExtractAudio",
                    "preferredcodec": codec,
                    "preferredquality": str(o.get("audio_quality", "0"))})
    else:
        quality = str(_get(o, "quality", cfg, "dl_quality", "best") or "best")
        compatible = _flag(_get(o, "compatible", cfg, "dl_compatible", True))
        if quality.lower() == "compatible":
            quality, compatible = "best", True
        if concat:
            compatible = True          # joining needs one codec across items
        if format_id:
            opts["format"] = f"{format_id}+ba/{format_id}"
        else:
            opts["format"], sort = format_choice(quality, compatible, container)
            if sort:
                opts["format_sort"] = sort
        opts["merge_output_format"] = "webm/mkv" if container == "webm" else container
        encoder = str(_get(o, "recode_encoder", cfg, "recode_encoder", "") or "")
        if encoder and not ffmpegtools.spec(encoder):
            encoder = ""
        if encoder:
            target = ffmpegtools.recode_container(encoder, container)
            final_ext = target
            pps.append({"key": "MTForceRecode", "encoder": encoder,
                        "quality": str(_get(o, "recode_quality", cfg, "recode_quality", "balanced")),
                        "container": container, "normalize": normalize})
        elif container in ("mp4", "mkv") and not format_id:
            # Audio-only sources (a radio show, a podcast) keep their format
            # instead of becoming a video file with no picture.
            keep = "/".join(f"{e}>{e}" for e in ("mp3", "m4a", "opus", "ogg", "flac", "wav", "aac"))
            pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": f"{keep}/{container}"})
            final_ext = container

        subs = subtitle_request(o, cfg)
        if subs:
            langs, auto = subs
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = auto
            opts["subtitleslangs"] = langs
            opts["subtitlesformat"] = "vtt/best" if container == "webm" else "srt/best"
            if _flag(_get(o, "embed_subs", cfg, "dl_embed_subs", True)):
                pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
            elif container != "webm":
                pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt",
                            "when": "before_dl"})

    # ------------------------------------------------------- chapters, cuts
    remove = [p.strip() for p in str(o.get("remove_chapters") or "").split(",") if p.strip()]
    if sponsor or remove:
        # One ModifyChapters for both, as the yt-dlp CLI does: a second one
        # would re-insert already-cut sponsor segments as bogus chapters.
        pps.append({"key": "ModifyChapters",
                    "remove_chapters_patterns": [_chapter_pattern(p) for p in remove],
                    "remove_sponsor_segments": cats if sponsor and sb_mode == "remove" else [],
                    "force_keyframes": _flag(o.get("force_keyframes"))})
    if mode == "video" and normalize and not any(p["key"] == "MTForceRecode" for p in pps):
        pps.append({"key": "MTNormalizeAudio",
                    "quality": str(_get(o, "recode_quality", cfg, "recode_quality", "high"))})

    # Metadata before cover art, as the CLI does: FFmpegMetadata after
    # EmbedThumbnail can strip the art again. Chapters are written by the same
    # step, so it runs when either is wanted.
    mark_chapters = sponsor and sb_mode == "mark"
    if embed_meta or embed_chapters or mark_chapters:
        pps.append({"key": "FFmpegMetadata", "add_metadata": embed_meta,
                    "add_chapters": bool(embed_chapters or mark_chapters)})

    embed_thumb = _flag(_get(o, "embed_thumbnail", cfg, "embed_thumbnail", True))
    from .recode import embeddable_exts
    known_bad = mode == "audio" and final_ext not in embeddable_exts()
    # A cover picture is an extra stream, and joined files must have
    # identical streams, so joining goes without cover art.
    if embed_thumb and not known_bad and not concat:
        opts["writethumbnail"] = True
        pps.append({"key": "MTEmbedThumbnail", "already_have_thumbnail": write_thumb})
    if write_thumb:
        # The format setting only converts a picture that is written anyway;
        # on its own it never leaves an image file nobody asked for.
        opts["writethumbnail"] = True

    if _flag(_get(o, "split_chapters", cfg, "dl_split_chapters", False)):
        pps.append({"key": "FFmpegSplitChapters", "force_keyframes": _flag(o.get("force_keyframes"))})
    if concat:
        playlist["concat"] = True
        pps.append({"key": "FFmpegConcat", "only_multi_video": False, "when": "playlist"})

    # ----------------------------------------------------------- extra files
    if write_json:
        opts["writeinfojson"] = True
    if _flag(_get(o, "write_description", cfg, "write_description", False)):
        opts["writedescription"] = True
    if _flag(_get(o, "write_comments", cfg, "write_comments", False)):
        opts["getcomments"] = True
        # Uncapped comment extraction never finishes on a popular video.
        try:
            limit = int(float(_get(o, "max_comments", cfg, "dl_max_comments", 200) or 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 100000))
        opts["_comments"] = limit
        # max-comments, max-parents, max-replies, max-replies-per-thread:
        # "How many" counts top-level comments, and skipping the replies
        # saves one request per thread.
        opts.setdefault("extractor_args", {}).setdefault("youtube", {}).update({
            "max_comments": [str(limit), str(limit), "0", "0"],
            "comment_sort": ["top"],
        })
    if _flag(o.get("write_all_thumbnails")):
        opts["write_all_thumbnails"] = True
    if _flag(_get(o, "write_link", cfg, "dl_write_link", False)):
        opts["writeurllink"] = os.name == "nt"
        opts["writelink"] = os.name != "nt"

    # -------------------------------------------------------------- sections
    ranges, chapter_names = parse_sections(o.get("section", ""))
    if ranges or chapter_names:
        opts["_missed_chapters"] = missed = []
        opts["download_ranges"] = _Ranges(chapter_names, ranges, missed)
        opts["force_keyframes_at_cuts"] = _flag(o.get("force_keyframes", True))

    # Live streams belong to the Live tab; premieres are waited for by
    # run_download itself, where Cancel works (yt-dlp's own wait cannot be
    # interrupted).
    if final_ext:
        opts["final_ext"] = final_ext
    opts["postprocessors"] = pps
    opts["_playlist"] = playlist
    opts["_mode"] = mode
    opts["_container"] = container
    opts["_notes"] = notes
    return opts


# ------------------------------------------------------------------ probing

def _has_video_and_list(url: str) -> bool:
    """watch?v=X&list=Y and youtu.be/X?list=Y: a video that sits in a playlist."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    q = parse_qs(parts.query)
    if "list" not in q:
        return False
    host = (parts.hostname or "").lower()
    if "v" in q:
        return True
    if host.endswith("youtu.be") and parts.path.strip("/"):
        return True
    return bool(re.match(r"^/(shorts|live)/[\w-]+", parts.path or ""))


def probe(url: str, flat_playlist: bool = True) -> dict:
    """Metadata for the preview card. Never downloads.

    A playlist or channel is read flat and only as far as its first 200
    entries: a large channel would otherwise take a minute to preview.
    """
    in_playlist = _has_video_and_list(url)
    opts = base_opts()
    opts.update({"noplaylist": in_playlist, "skip_download": True,
                 "extract_flat": "in_playlist" if flat_playlist else False,
                 "playlistend": 200, "ignore_no_formats_error": True})
    with session(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise errors.AppError("unavailable", f"Nothing could be read from {url}")
    out = summarize(info)
    out["in_playlist"] = bool(in_playlist and out.get("kind") == "video")
    if not out.get("site"):
        out["site"] = errors.site_name(url)
    return out


_CHANNEL_PATH = re.compile(r"/(@[^/]+|channel/|c/|user/)", re.I)


def summarize(info: dict) -> dict:
    """The preview card's facts for a video or a playlist."""
    url = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
    site = errors.site_name(url) or str(info.get("extractor_key") or "")
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        known = info.get("playlist_count")
        thumbs = info.get("thumbnails") or []
        thumb = (thumbs[-1].get("url", "") if thumbs else "") or \
            ((entries[0].get("thumbnails") or [{}])[-1].get("url", "") if entries else "") or \
            (entries[0].get("thumbnail", "") if entries else "")
        return {
            "kind": "playlist",
            "id": info.get("id", ""),
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "count": known or len(entries),
            "count_more": bool(not known and len(entries) >= 200),
            "url": url,
            "site": site,
            "is_channel": bool(_CHANNEL_PATH.search(urlsplit(url).path or "")) if url else False,
            "thumbnail": thumb,
            "entries": [{"title": e.get("title") or "Untitled",
                         "url": e.get("url") or e.get("webpage_url") or "",
                         "duration": e.get("duration") or 0,
                         "live_status": e.get("live_status") or ""} for e in entries[:200]],
        }

    formats = info.get("formats") or []
    heights = sorted({f.get("height") for f in formats if f.get("height")}, reverse=True)
    manual = sorted(k for k in (info.get("subtitles") or {}) if k != "live_chat")
    auto = sorted((info.get("automatic_captions") or {}).keys())
    fps = [f.get("fps") for f in formats if f.get("height") and f.get("fps")]
    by_height, audio, size_best, size_smallest = _sizes(formats, info.get("duration"))
    return {
        "kind": "video",
        "id": info.get("id", ""),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "duration_string": info.get("duration_string") or _dur(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "url": url,
        "site": site,
        "is_channel": False,
        "extractor": info.get("extractor_key") or "",
        "upload_date": _date(info.get("upload_date")),
        "view_count": info.get("view_count") or 0,
        "is_live": bool(info.get("is_live")),
        "live_status": info.get("live_status") or "",
        "release_timestamp": info.get("release_timestamp") or None,
        "description": (info.get("description") or "")[:600],
        "heights": heights,
        "max_fps": int(round(max(fps))) if fps else 0,
        "size_by_height": by_height,        # keyed by the Quality values
        "size_best": size_best,
        "size_smallest": size_smallest,
        "audio_size": audio,
        "subtitles": manual,
        "auto_captions": auto,
        "language": info.get("language") or "",
        "has_captions": bool(manual or auto),
        "filesize_approx": info.get("filesize_approx") or 0,
    }


def _fmt_size(f: dict, duration) -> int:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = f["tbr"] * 1000 / 8 * duration
    return int(size or 0)


def _direct(f: dict) -> bool:
    return not str(f.get("protocol") or "").startswith(("m3u8", "http_dash_segments"))


_CAPS = (2160, 1440, 1080, 720, 480, 360)


def _res(f: dict) -> int:
    """The picture's smaller side: what the quality presets cap."""
    w, h = f.get("width"), f.get("height")
    return int(min(w, h) if w and h else (h or 0))


def _sizes(formats: list[dict], duration) -> tuple[dict[int, int], int, int, int]:
    """Size estimates for the size chip: ({quality cap: bytes}, sound bytes,
    best bytes, smallest bytes).

    Keyed by the Quality values (2160, 1080, ...) and worked out the way the
    presets choose: the largest picture whose smaller side fits the cap, so a
    1920x800 film counts for "1080p". Follows the default choice: a direct
    (non-HLS) stream, H.264 where the site has it, plus the best sound in the
    video's own language. HLS sizes are guesses from the bitrate and run high,
    so they count only when nothing else exists at that size.
    """
    audio = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"):
            if (f.get("language_preference") or 0) < -1 or not _direct(f):
                continue                      # dubbed tracks, HLS guesses
            audio = max(audio, _fmt_size(f, duration))
    groups: dict[int, list[dict]] = {}
    for f in formats:
        if _res(f) and f.get("vcodec") not in (None, "none") and _fmt_size(f, duration):
            groups.setdefault(_res(f), []).append(f)

    def size_at(res: int) -> int:
        pool = [f for f in groups[res] if _direct(f)] or groups[res]
        h264 = [f for f in pool if codec_label(f.get("vcodec")) == "H.264"]
        pick = max(h264 or pool, key=lambda f: _fmt_size(f, duration))
        size = _fmt_size(pick, duration)
        return size + audio if pick.get("acodec") in (None, "none") else size

    by_cap: dict[int, int] = {}
    for cap in _CAPS:
        fits = [r for r in groups if r <= cap]
        if fits:
            by_cap[cap] = size_at(max(fits))
    best = size_at(max(groups)) if groups else 0
    smallest = size_at(min(groups)) if groups else 0
    return by_cap, audio, best, smallest


def _dur(seconds: float) -> str:
    s = int(seconds or 0)
    if not s:
        return ""
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ------------------------------------------------------------ format table

_VCODECS = (("av01", "AV1"), ("av1", "AV1"), ("vp09", "VP9"), ("vp9", "VP9"), ("vp8", "VP8"),
            ("avc", "H.264"), ("h264", "H.264"), ("hvc", "H.265"), ("hev", "H.265"),
            ("h265", "H.265"), ("hevc", "H.265"), ("theora", "Theora"))
_ACODECS = (("opus", "Opus"), ("mp4a", "AAC"), ("aac", "AAC"), ("mp3", "MP3"),
            ("vorbis", "Vorbis"), ("flac", "FLAC"), ("ec-3", "Dolby Digital Plus"),
            ("eac3", "Dolby Digital Plus"), ("ac-3", "Dolby Digital"), ("ac3", "Dolby Digital"),
            ("alac", "ALAC"), ("pcm", "PCM"))


def _codec_name(raw, table) -> str:
    raw = str(raw or "").lower()
    if raw in ("", "none"):
        return ""
    for prefix, name in table:
        if raw.startswith(prefix):
            return name
    return raw.split(".")[0].upper()


def codec_label(vcodec) -> str:
    """'H.264', 'VP9', 'AV1', ... for a yt-dlp vcodec string."""
    return _codec_name(vcodec, _VCODECS)


def human_size(n) -> str:
    n = float(n or 0)
    if n <= 0:
        return ""
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            value = n / div
            return f"{value:.1f} {unit}" if value < 10 and unit == "GB" else f"{value:.0f} {unit}"
    return f"{n:.0f} B"


def _format_rows(info: dict) -> list[dict]:
    duration = info.get("duration")
    formats = [f for f in (info.get("formats") or [])
               if f.get("format_id") not in (None, "source")
               and "storyboard" not in str(f.get("format_note") or "").lower()
               and f.get("ext") != "mhtml"
               and not (f.get("vcodec") == "none" and f.get("acodec") == "none")]
    languages = {str(f.get("language") or "") for f in formats
                 if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")}
    known_audio = any(f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
                      for f in formats)
    rows: dict[tuple, dict] = {}
    for f in formats:
        has_v = f.get("vcodec") not in (None, "none") or bool(f.get("height"))
        has_a = f.get("acodec") not in (None, "none")
        if not has_v and not has_a and known_audio:
            continue            # a stream that says nothing about itself (HLS twins)
        kind = "av" if has_v and has_a else ("video" if has_v else "audio")
        vc, ac = _codec_name(f.get("vcodec"), _VCODECS), _codec_name(f.get("acodec"), _ACODECS)
        size = _fmt_size(f, duration)
        fps = int(round(f.get("fps") or 0))
        hdr = str(f.get("dynamic_range") or "SDR")
        note = str(f.get("format_note") or "")
        lang_label = note.split(",")[0].strip() if note and len(languages) > 1 else ""
        if kind == "audio":
            parts = ["Audio", ac or (f.get("ext") or "").upper()]
            if f.get("abr"):
                parts.append(f"{round(f['abr'])} kbps")
            if size:
                parts.append(human_size(size))
            if lang_label:
                parts.append(lang_label)
            key = ("audio", ac, str(f.get("language") or ""), round((f.get("abr") or 0) / 16))
        else:
            # The smaller side, as the Quality presets count: a 1080x1920 Short is 1080p.
            parts = [f"{_res(f)}p" if f.get("height") else (f.get("resolution") or "Video")]
            if fps >= 50:
                parts.append(f"{fps} fps")
            parts.append(f"{vc} + {ac}" if kind == "av" and ac else (vc or (f.get("ext") or "").upper()))
            if hdr != "SDR":
                parts.append("HDR")
            if size:
                parts.append(human_size(size))
            if kind == "video":
                parts.append("video only, best audio added")
            key = (kind, f.get("height"), fps >= 50, vc, hdr)
        row = {
            "format_id": f.get("format_id", ""),
            "label": " · ".join(p for p in parts if p),
            "kind": kind,
            "ext": f.get("ext", ""),
            "resolution": f.get("resolution") or (
                f"{f.get('width')}x{f.get('height')}" if f.get("height") else "audio only"),
            "height": f.get("height") or 0,
            "fps": fps,
            "vcodec": (f.get("vcodec") or "none").split(".")[0],
            "acodec": (f.get("acodec") or "none").split(".")[0],
            "vcodec_name": vc,
            "acodec_name": ac,
            "abr": round(f.get("abr") or 0),
            "tbr": round(f.get("tbr") or 0),
            "filesize": size,
            "proto": f.get("protocol", ""),
            "note": note,
            "language": f.get("language") or "",
            "dynamic_range": f.get("dynamic_range") or "",
        }
        best = rows.get(key)
        # Duplicates (the same stream over DASH and HLS, a "drc" twin with
        # squashed dynamics): keep the plain direct download, then the
        # higher bitrate.
        rank = (_direct(f), "drc" not in str(row["format_id"]).lower(), row["tbr"],
                row["filesize"])
        if best is None or rank > best["_rank"]:
            row["_rank"] = rank
            rows[key] = row
    out = sorted(rows.values(), key=lambda r: (r["kind"] != "audio", r["height"], r["fps"],
                                               r["tbr"]), reverse=True)
    for r in out:
        r.pop("_rank", None)
    return out


def list_formats(url: str) -> dict:
    """The streams one video offers, with plain labels, for the exact-format
    picker. A playlist is refused after reading only its first page."""
    opts = base_opts()
    opts.update({"noplaylist": True, "skip_download": True, "extract_flat": "in_playlist",
                 "playlistend": 1, "ignore_no_formats_error": True})
    with session(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise errors.AppError("unavailable", f"Nothing could be read from {url}")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise errors.AppError("bad_link", "Formats are listed per video, and this link is a "
                              "playlist or channel. Open one video and paste its link.")
    return {"title": info.get("title", ""), "formats": _format_rows(info)}


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


# ------------------------------------------------------------ progress text

_PP_STAGES = {
    "Merger": "Joining video and audio…",
    "EmbedThumbnail": "Adding cover art…",
    "MTEmbedThumbnail": "Adding cover art…",
    "SafeEmbedThumbnail": "Adding cover art…",
    "EmbedSubtitle": "Adding subtitles…",
    "SubtitlesConvertor": "Adding subtitles…",
    "ForceRecode": "Re-encoding. This can take a while…",
    "NormalizeAudio": "Evening out the volume…",
    "MoveFiles": "Saving…",
    "SplitChapters": "Splitting into chapters…",
    "Concat": "Joining the videos into one file…",
    "SponsorBlock": "Looking up sponsor segments…",
}


def pp_stage(key: str, *, codec: str = "", ext: str = "", metadata: bool = True,
             cutting: str = "sponsor") -> str:
    """Plain words for a postprocessor; never a class name."""
    if key in ("ExtractAudio", "NormalizedExtractAudio"):
        return f"Converting to {(codec or 'audio').upper()}…"
    if key in ("VideoRemuxer", "VideoConvertor"):
        return f"Converting to {(ext or 'the chosen format').upper()}…"
    if key == "Metadata":
        return "Adding title and artist…" if metadata else "Adding chapter markers…"
    if key == "ModifyChapters":
        return {"sponsor": "Cutting sponsor segments…", "mark": "Marking sponsor segments…",
                "chapters": "Removing chapters…"}.get(cutting, "Cutting sponsor segments…")
    return _PP_STAGES.get(key, "Finishing up…")


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


def job_temp_dir(url: str, options: dict) -> str:
    """Staging folder for one request. The same link with the same choices
    (a retry, or Resume after Cancel) gets the same folder, so yt-dlp
    continues its .part files instead of starting over."""
    keep = {k: v for k, v in sorted((options or {}).items())
            if not str(k).startswith("_") and k not in ("output_dir", "output_template")}
    digest = hashlib.sha1(json.dumps([url, keep], sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()[:12]
    return str(Path(temp_dir()) / f"dl-{digest}")


# Staging folders in use, so the same link queued twice never has two
# yt-dlp instances writing one .part file. Folders left behind by a cancel
# are kept for Resume, then removed after a few days: a cancelled 4K video
# would otherwise sit in the temp folder for good.
_STAGING_MAX_AGE = 3 * 24 * 3600
_staging_lock = threading.Lock()
_staging_busy: dict[str, str] = {}
_staging_pruned = 0.0


def _newest_mtime(folder: Path) -> float:
    newest = 0.0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            with contextlib.suppress(OSError):
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
    with contextlib.suppress(OSError):
        newest = max(newest, folder.stat().st_mtime)
    return newest


def _prune_staging() -> None:
    global _staging_pruned
    now = time.time()
    with _staging_lock:
        if now - _staging_pruned < 3600:
            return
        _staging_pruned = now
        busy = {os.path.normcase(p) for p in _staging_busy}
    try:
        folders = [p for p in Path(temp_dir()).glob("dl-*") if p.is_dir()]
    except OSError:
        return
    for folder in folders:
        if os.path.normcase(str(folder)) in busy:
            continue
        if now - _newest_mtime(folder) > _STAGING_MAX_AGE:
            shutil.rmtree(folder, ignore_errors=True)


def claim_staging(path: str, jid: str) -> str:
    """The staging folder this job may use: ``path``, or a folder of its own
    while another running job already works in ``path``."""
    with _staging_lock:
        owner = _staging_busy.get(path)
        if owner and owner != jid:
            path = f"{path}-{jid}"
        _staging_busy[path] = jid
    return path


def release_staging(path: str | None) -> None:
    if path:
        with _staging_lock:
            _staging_busy.pop(path, None)


# ----------------------------------------------------------------- the run

_SIDE_KINDS = (
    (".description.txt", "description", "Description"),
    (".comments.txt", "other", "Top comments"),
    (".info.json", "json", "Technical details"),
    (".url", "link", "Shortcut to the page"),
    (".webloc", "link", "Shortcut to the page"),
    (".desktop", "link", "Shortcut to the page"),
)
_SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".lrc", ".ttml")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# Unfinished downloads, never attached to a job.
_PARTIAL = re.compile(r"(\.part(?:-Frag\d+)?(?:\.part)?|\.ytdl)$", re.I)
# yt-dlp's per-stream and scratch names ("x.f399.mp4", "x.temp.mp4"), only
# ever deleted when this job wrote them moments ago.
_INTERMEDIATE = re.compile(r"\.(?:f[\w-]+\.\w{2,5}|temp\.\w{2,5}|orig\.\w{2,5})"
                           r"(?:\.part(?:-Frag\d+)?(?:\.part)?|\.ytdl)?$", re.I)


class _Log:
    """yt-dlp logger: errors are attributed to the playlist item being
    processed, a few debug lines carry facts the hooks do not."""

    def __init__(self, run: "_Run"):
        self.run = run

    def debug(self, msg: str) -> None:
        if "larger than max-filesize" in msg or "smaller than min-filesize" in msg:
            self.run.size_abort(msg)

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.run.warnings.append(str(msg))
        del self.run.warnings[:-20]

    def error(self, msg: str) -> None:
        self.run.on_error(str(msg))


class _Run:
    """State of one download job while yt-dlp works through it."""

    def __init__(self, jid: str, url: str, options: dict, opts: dict, outdir: str):
        self.jid, self.url, self.options, self.opts, self.outdir = jid, url, options, opts, outdir
        self.started = time.time()
        self.filters: Filters = opts.pop("_filters")
        self.playlist_opts: dict = opts.pop("_playlist")
        self.mode: str = opts.pop("_mode")
        self.container: str = opts.pop("_container")
        self.notes: list[str] = opts.pop("_notes")
        self.comments = opts.pop("_comments", 0)
        self.missed: list[str] = opts.pop("_missed_chapters", [])
        self.pp_specs: list[dict] = opts.pop("postprocessors", [])
        self.is_playlist = False
        self.meta: dict = {}
        self.count = 0                         # items selected in a playlist
        self.current: dict | None = None       # playlist item being processed
        self.rejects: dict[str, str] = {}
        self.archived: set[str] = set()
        self.failed: dict[str, dict] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.size_skips: list[str] = []
        self.saved: set[str] = set()
        self.reused = 0
        self.files: list[tuple[str, str, str]] = []
        self.partials: set[str] = set()
        self.planned: set[str] = set()         # final names; never cleaned up
        self.stop_reason: str | None = None
        # progress of the current item
        self.item_index = 0
        self.streams: dict[str, dict] = {}
        self.item_progress = 0.0
        self.last_update = 0.0
        self.meta_sent = False

    # ------------------------------------------------------------ helpers
    def _key(self, info: dict) -> str:
        return str(info.get("id") or info.get("url") or info.get("webpage_url") or "")

    def cancelled(self) -> bool:
        return jobs.cancelled(self.jid)

    def note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    # ------------------------------------------------------ yt-dlp hooks
    def _count_from(self, info: dict) -> None:
        """How many items the playlist run selected: yt-dlp only tells its
        per-item callbacks (n_entries), after items, first/last and limits."""
        if self.count or not self.is_playlist:
            return
        try:
            n = int(info.get("n_entries") or 0)
        except (TypeError, ValueError):
            return
        if n:
            limit = self.opts.get("max_downloads")
            self.count = min(n, int(limit)) if limit else n

    def _enter(self, info) -> None:
        """Remember which playlist item yt-dlp is on, so an error it logs
        next is charged to that item."""
        if not self.is_playlist or not info.get("playlist_autonumber") or \
                info.get("_type") in ("playlist", "multi_video"):
            return
        self._count_from(info)
        key = self._key(info)
        title = info.get("title") or ""
        url = info.get("webpage_url") or info.get("url") or ""
        if self.current and self.current["id"] == key:
            self.current["title"] = title or self.current["title"]
            self.current["url"] = self.current["url"] or url
            return
        self.current = {"id": key, "title": title, "url": url}

    def match_filter(self, info: dict, incomplete=False):
        jobs.raise_if_cancelled(self.jid)
        if incomplete is True:
            self._enter(info)
        reason = self.filters.check(info, incomplete)
        if reason:
            self.rejects.setdefault(self._key(info), reason)
        return reason

    def wrap_archive(self, ydl) -> None:
        """Count items skipped because the archive already lists them, and
        follow the playlist item being worked on. yt-dlp asks the archive
        about every item first, before any filter: for sites that cannot
        tell a video link from a playlist link it skips the match filter
        for the unread entry, so that alone would miss items."""
        original = ydl.in_download_archive

        def in_archive(info_dict):
            jobs.raise_if_cancelled(self.jid)      # before each item, on every site
            self._enter(info_dict)
            hit = original(info_dict)
            if hit and info_dict.get("_type") not in ("playlist", "multi_video"):
                self._count_from(info_dict)
                self.archived.add(str(info_dict.get("id") or ""))
            return hit
        ydl.in_download_archive = in_archive

    def on_error(self, msg: str) -> None:
        text = re.sub(r"^ERROR:\s*", "", msg.strip())
        try:
            sys.stderr.write(f"[{self.jid}] {errors.redact(text)[:2000]}\n")
        except Exception:
            pass
        if self.is_playlist and self.current:
            key = self.current["id"]
            if key and key not in self.failed and key not in self.saved:
                err = errors.classify(RuntimeError(text), self.current["url"] or self.url, "download")
                self.failed[key] = {"title": self.current["title"], "url": self.current["url"],
                                    "error": err.get("title", ""), "code": err.get("code", ""),
                                    "detail": err.get("detail", "")}
                return
        self.errors.append(text)

    def size_abort(self, msg: str) -> None:
        if self.filters.max_bytes:
            self.size_skips.append(f"larger than your {self.filters._max_label} limit")
        else:
            self.size_skips.append("outside the size limits you set")

    def on_video(self, info: dict, planned: str = "") -> None:
        """A download is about to start (the 'video' stage, after the format
        was chosen): the formats and their sizes, the item's place, and the
        name its files will get (for cleaning up after Cancel or a failure)."""
        jobs.raise_if_cancelled(self.jid)
        if planned:
            self.partials.add(planned)
            self.planned.add(planned)
        parts = info.get("requested_formats") or [info]
        self.streams = {}
        for i, f in enumerate(parts):
            fid = str(f.get("format_id") or i)
            has_video = f.get("vcodec") not in (None, "none")
            self.streams[fid] = {"est": _fmt_size(f, info.get("duration")) or 0, "total": 0,
                                 "done": 0, "finished": False,
                                 "video": has_video or len(parts) == 1}
        self.item_progress = 0.0
        fields: dict[str, Any] = {"stage": "Downloading", "indeterminate": True,
                                  "speed": "", "eta": "", "speed_bps": None, "eta_s": None}
        fmt = " + ".join(f"{p.get('format_id')}" for p in parts if p.get("format_id"))
        codecs = " · ".join(x for x in (info.get("resolution") if info.get("height") else "",
                                        _codec_name(info.get("vcodec"), _VCODECS),
                                        _codec_name(info.get("acodec"), _ACODECS)) if x)
        fields["stage_detail"] = " · ".join(x for x in (fmt, codecs) if x)
        if self.is_playlist:
            index = int(info.get("playlist_autonumber") or self.item_index + 1)
            self.item_index = index
            fields["item"] = {"index": index, "count": self.count or index,
                              "title": info.get("title") or ""}
            fields["progress"] = min(0.999, (index - 1) / max(1, self.count or index))
        elif not self.meta_sent:
            self.meta_sent = True
            fields.update({k: v for k, v in (("title", info.get("title")),
                                             ("thumbnail", info.get("thumbnail")),
                                             ("uploader", info.get("uploader") or info.get("channel")))
                           if v})
        jobs.update(self.jid, **fields)
        self.last_update = time.time()

    def _scale(self, item_fraction: float) -> float:
        if not self.is_playlist:
            return item_fraction
        count = max(1, self.count or self.item_index or 1)
        return min(0.999, ((max(1, self.item_index) - 1) + item_fraction) / count)

    def progress(self, d: dict) -> None:
        jobs.raise_if_cancelled(self.jid)
        status = d.get("status")
        info = d.get("info_dict") or {}
        fid = str(info.get("format_id") or "")
        for key in ("tmpfilename", "filename"):
            if d.get(key):
                self.partials.add(str(d[key]))
        if fid not in self.streams:
            if "+" in fid and len(self.streams) > 1:
                # ffmpeg fetching picture and sound together reports them as one.
                est = sum(x["est"] for x in self.streams.values())
                self.streams = {fid: {"est": est, "total": 0, "done": 0, "finished": False,
                                      "video": True}}
            else:
                return                  # subtitles, thumbnails: not the media
        s = self.streams[fid]
        if status == "finished":
            s["finished"] = True
            s["total"] = s["done"] = int(d.get("total_bytes") or d.get("downloaded_bytes")
                                         or s["total"] or s["est"] or 0)
        elif status == "downloading":
            s["done"] = int(d.get("downloaded_bytes") or 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                s["total"] = int(total)
        else:
            return
        now = time.time()
        if status == "downloading" and now - self.last_update < _UPDATE_EVERY:
            return
        self.last_update = now

        streams = list(self.streams.values())
        sizes = [x["total"] or x["est"] for x in streams]
        done = sum(x["done"] for x in streams)
        if all(sizes):
            fraction = min(1.0, done / max(1, sum(sizes)))
            bytes_total = sum(sizes)
        else:
            # Unknown sizes: picture first up to 85 % of the bar, then sound.
            bytes_total = None
            this = s["done"] / s["total"] if s["total"] else 0.0
            if len(streams) == 1:
                fraction = this
            elif s["video"]:
                fraction = 0.85 * this
            else:
                fraction = 0.85 + 0.15 * this
        self.item_progress = max(self.item_progress, _DOWNLOAD_SHARE * fraction)
        speed = d.get("speed")
        eta = d.get("eta")
        if bytes_total and speed:
            eta = max(0, (bytes_total - done) / speed)
        jobs.update(self.jid, stage="Downloading", indeterminate=bytes_total is None and not s["total"],
                    progress=self._scale(self.item_progress),
                    bytes_done=done, bytes_total=bytes_total,
                    speed_bps=float(speed) if speed else None,
                    eta_s=int(eta) if eta is not None else None,
                    speed=_fmt_speed(speed), eta=_fmt_eta(eta))

    def pp_hook(self, d: dict) -> None:
        jobs.raise_if_cancelled(self.jid)
        key = str(d.get("postprocessor") or "")
        info = d.get("info_dict") or {}
        if d.get("status") == "started":
            if key == "RequestName" or (key == "MoveFiles" and
                                        self.opts.get("paths", {}).get("temp") is None):
                return
            if key in ("SponsorBlock", "ThumbnailsConvertor", "SubtitlesConvertor"):
                # These run before the download; the bar has not moved yet.
                if key == "SponsorBlock":
                    jobs.update(self.jid, stage=pp_stage(key), indeterminate=True)
                return
            cutting = "chapters"
            if any(p["key"] == "SponsorBlock" for p in self.pp_specs):
                removes = any(p.get("remove_sponsor_segments") for p in self.pp_specs
                              if p["key"] == "ModifyChapters")
                cutting = "sponsor" if removes else "mark"
            meta = next((p.get("add_metadata", True) for p in self.pp_specs
                         if p["key"] == "FFmpegMetadata"), True)
            codec = next((p.get("preferredcodec", "") for p in self.pp_specs
                          if p["key"] in ("FFmpegExtractAudio", "MTExtractAudio")), "")
            ext = self.opts.get("final_ext") or info.get("ext") or ""
            stage = pp_stage(key, codec=codec, ext=ext, metadata=meta, cutting=cutting)
            progress = self._scale(max(self.item_progress, _DOWNLOAD_SHARE))
            jobs.update(self.jid, stage=stage, indeterminate=True, progress=progress,
                        speed="", eta="", speed_bps=None, eta_s=None)
        elif d.get("status") == "finished":
            if key == "SplitChapters":
                for n, ch in enumerate(info.get("chapters") or [], 1):
                    if ch.get("filepath"):
                        self.files.append((str(ch["filepath"]), "",
                                           f"Chapter {n}: {ch.get('title') or ''}".strip(": ")))
            elif key == "MoveFiles":
                self._collect(info)

    def _collect(self, info: dict) -> None:
        """The item is in its final place: remember it and its extra files."""
        filepath = info.get("filepath")
        if not filepath:
            return
        final_dir = info.get("__finaldir") or os.path.dirname(filepath)
        moved = info.get("__files_to_move") or {}
        main = moved.get(filepath) or os.path.join(final_dir, os.path.basename(filepath))
        self.saved.add(self._key(info) or main)
        if info.get("__mt_reused"):
            self.reused += 1
        self.files.append((main, "", ""))
        ext = os.path.splitext(main)[1].lstrip(".").lower()
        if self.container == "webm" and self.mode == "video" and ext == "mkv":
            self.note("WebM can't hold this video's format, so it was saved as MKV.")
        stem = os.path.splitext(os.path.basename(main))[0]
        folder = os.path.dirname(main)
        if self.comments and info.get("comments"):
            path = os.path.join(folder, f"{stem}.comments.txt")
            if _write_comments(path, info["comments"], self.comments):
                self.files.append((path, "other", "Top comments"))
        for name in _siblings(folder, stem, os.path.basename(main)):
            path = os.path.join(folder, name)
            low = name.lower()
            if low.endswith(".description"):
                target = path + ".txt"
                with contextlib.suppress(OSError):
                    os.replace(path, target)
                    self.files.append((target, "description", "Description"))
                continue
            for suffix, kind, label in _SIDE_KINDS:
                if low.endswith(suffix):
                    if suffix != ".comments.txt":
                        self.files.append((path, kind, label))
                    break
            else:
                if low.endswith(_SUB_EXTS):
                    lang = name[len(stem) + 1:].rsplit(".", 1)[0]
                    self.files.append((path, "subtitles", f"Subtitles ({lang})" if lang else "Subtitles"))
                elif low.endswith(_IMG_EXTS):
                    self.files.append((path, "thumbnail", "Thumbnail"))

    # --------------------------------------------------------- the ending
    def attach(self) -> int:
        seen: set[str] = set()
        # The media itself first, then chapters and extra files.
        ordered = sorted(self.files, key=lambda f: 0 if not f[1] and not f[2] else 1)
        for path, kind, label in ordered:
            norm = os.path.normcase(os.path.abspath(path))
            if norm in seen or not os.path.isfile(path) or _PARTIAL.search(path):
                continue
            seen.add(norm)
            jobs.add_file(self.jid, path, kind, label)
        return len(seen)

    def cleanup_partials(self) -> None:
        """After Cancel or a failure with staging off: remove this job's
        unfinished files and the cover images and subtitles it wrote for
        media that never arrived. Only files written during this job and
        never the media itself. Staged files stay in temp for Resume."""
        if self.opts.get("paths", {}).get("temp"):
            return

        def norm(p: str) -> str:
            return os.path.normcase(os.path.abspath(p))

        # Saved files and the final names yt-dlp chose are never touched: a
        # title such as "Report.final" would otherwise look like one of
        # yt-dlp's per-stream names ("x.f399.mp4").
        keep = {norm(p) for p, _, _ in self.files} | {norm(p) for p in self.planned}
        stems = set()
        for p in self.partials:
            name = os.path.basename(p)
            bare = _INTERMEDIATE.sub("", _PARTIAL.sub("", name))
            stems.add((os.path.dirname(p), os.path.splitext(bare)[0]))
            if norm(p) in keep:
                continue
            if (_PARTIAL.search(name) or _INTERMEDIATE.search(name)) and os.path.isfile(p):
                with contextlib.suppress(OSError):
                    os.remove(p)
        for folder, stem in stems:
            for name in _siblings(folder, stem, ""):
                path = os.path.join(folder, name)
                if norm(path) in keep:
                    continue
                try:
                    fresh = os.path.getmtime(path) >= self.started - 2
                except OSError:
                    continue
                low = name.lower()
                if fresh and (_PARTIAL.search(name) or _INTERMEDIATE.search(name)
                              or low.endswith(_IMG_EXTS + _SUB_EXTS)):
                    with contextlib.suppress(OSError):
                        os.remove(path)
        _drop_empty_folders(self.partials, self.outdir)

    def result(self, info: dict | None) -> dict:
        items_failed = [dict(v) for v in self.failed.values()]
        return {
            "meta": self.meta,
            "output_dir": self.outdir,
            "count": len(self.saved),
            "saved": len(self.saved),
            "archived": len(self.archived),
            "failed": len(items_failed),
            "items_failed": items_failed,
            "skipped": len(self.rejects),
            "stop_reason": self.stop_reason,
            "notes": list(self.notes),
            "playlist": self.is_playlist,
        }

    def nothing_saved(self) -> None:
        """Raise the right outcome for a run that saved nothing: the real
        error when something failed, otherwise Skipped with the reason."""
        n_arch = len(self.archived)
        if self.failed:
            first = next(iter(self.failed.values()))
            raise RuntimeError(first.get("detail") or first.get("error") or "Nothing could be downloaded")
        if self.errors and not self.rejects and not n_arch:
            raise RuntimeError(self.errors[0])
        if self.stop_reason == "up_to_date" and not self.rejects:
            raise jobs.Skipped("Nothing new. You already have the latest video.")
        if n_arch and not self.rejects:
            raise jobs.Skipped("Nothing new. The video was already downloaded." if n_arch == 1 else
                               f"Nothing new. All {n_arch:,} videos were already downloaded.")
        if self.rejects:
            n_rej = len(self.rejects)
            if not self.is_playlist or (n_rej == 1 and not n_arch):
                raise jobs.Skipped(f"Skipped: {next(iter(self.rejects.values()))}")
            if n_arch:
                raise jobs.Skipped(f"Nothing new. {n_arch:,} already downloaded, "
                                   f"{n_rej:,} didn't match your filters.")
            raise jobs.Skipped(f"Skipped: none of the {n_rej:,} videos matched your filters")
        if self.missed:
            names = ", ".join(f"“{n}”" for n in self.missed)
            raise jobs.Skipped(f"Skipped: no chapter is named {names}")
        if self.size_skips:
            raise jobs.Skipped(f"Skipped: {self.size_skips[0]}")
        if self.is_playlist and not self.count:
            raise jobs.Skipped("Nothing to download. This playlist has no videos.")
        raise RuntimeError(self.errors[0] if self.errors else
                           (self.warnings[-1] if self.warnings else "Nothing was downloaded."))


def _drop_empty_folders(paths, root: str) -> None:
    """Remove playlist sub-folders this job left empty (a cancel, or items
    joined into one file). Never the download folder itself, never a folder
    with anything in it."""
    base = os.path.normcase(os.path.abspath(root)).rstrip("\\/") + os.sep
    for folder in {os.path.dirname(os.path.abspath(p)) for p in paths}:
        if os.path.normcase(folder).startswith(base):
            with contextlib.suppress(OSError):
                os.rmdir(folder)


def _siblings(folder: str, stem: str, exclude: str) -> list[str]:
    """Files next to the main file that belong to it: 'stem.<something>'."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    prefix = stem + "."
    return [n for n in names if n.startswith(prefix) and n != exclude]


def _write_comments(path: str, comments: list[dict], limit: int) -> bool:
    """A readable top-comments file; the info .json is 100+ KB of formats."""
    def line(c: dict, indent: str) -> str:
        head = " · ".join(x for x in (
            str(c.get("author") or "Someone"),
            f"{c['like_count']:,} likes" if isinstance(c.get("like_count"), int) else "",
            str(c.get("_time_text") or "")) if x)
        text = str(c.get("text") or "").strip().replace("\n", "\n" + indent)
        return f"{indent}{head}\n{indent}{text}\n"
    top = [c for c in comments if (c.get("parent") or "root") == "root"][:limit]
    replies: dict[str, list[dict]] = {}
    for c in comments:
        if (c.get("parent") or "root") != "root":
            replies.setdefault(str(c["parent"]), []).append(c)
    out = []
    for c in top:
        out.append(line(c, ""))
        for r in replies.get(str(c.get("id")), [])[:5]:
            out.append(line(r, "    "))
    if not out:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        return True
    except OSError:
        return False


def _make_pps(ydl, run: _Run) -> None:
    """Instantiate the postprocessors in order: yt-dlp's by key, the app's
    from app/recode.py (with the job's cancel check wired in)."""
    from yt_dlp.postprocessor import get_postprocessor

    from . import recode

    def stop():
        jobs.raise_if_cancelled(run.jid)

    cuts = any(p["key"] == "ModifyChapters" and (p.get("remove_sponsor_segments")
                                                 or p.get("remove_chapters_patterns"))
               for p in run.pp_specs)
    ydl.add_post_processor(recode.RequestNamePP(ydl, on_video=run.on_video,
                                                recode_codec=_recode_codec(run.pp_specs),
                                                audio_only=run.mode == "audio", cuts=cuts),
                           when="video")
    for spec in run.pp_specs:
        spec = dict(spec)
        key, when = spec.pop("key"), spec.pop("when", "post_process")
        if key == "MTForceRecode":
            pp = recode.ForceRecodePP(ydl, should_stop=stop, **spec)
        elif key == "MTNormalizeAudio":
            pp = recode.NormalizeAudioPP(ydl, should_stop=stop, **spec)
        elif key == "MTExtractAudio":
            pp = recode.NormalizedExtractAudioPP(ydl, should_stop=stop, **spec)
        elif key == "MTEmbedThumbnail":
            pp = recode.SafeEmbedThumbnailPP(ydl, on_note=run.note, **spec)
        elif key == "FFmpegConcat":
            pp = recode.KeepOldConcatPP(ydl, should_stop=stop, **spec)
        else:
            pp = get_postprocessor(key)(ydl, **spec)
        if cuts and key in ("ModifyChapters", "FFmpegMetadata", "FFmpegSplitChapters"):
            pp = recode.skip_when_reused(pp)
        ydl.add_post_processor(pp, when=when)


def _recode_codec(pps: list[dict]) -> str:
    spec = next((p for p in pps if p["key"] == "MTForceRecode"), None)
    s = ffmpegtools.spec(spec["encoder"]) if spec else None
    return s["codec"] if s else ""


def _resolve(ydl, url: str) -> dict | None:
    """Read the link without processing it, following plain redirects, so
    the caller knows whether it is a playlist before anything downloads."""
    info = ydl.extract_info(url, download=False, process=False)
    for _ in range(5):
        if not info or info.get("_type") != "url":
            break
        info = ydl.extract_info(info["url"], download=False, process=False,
                                ie_key=info.get("ie_key"))
    return info


def _playlist_meta(ie: dict) -> dict:
    """Card facts for an unprocessed playlist. Its entries may be a lazy
    generator that the download itself still has to walk, so never touch them."""
    url = ie.get("webpage_url") or ie.get("original_url") or ""
    thumbs = ie.get("thumbnails") or []
    return {
        "kind": "playlist",
        "id": ie.get("id", ""),
        "title": ie.get("title") or "Playlist",
        "uploader": ie.get("uploader") or ie.get("channel") or "",
        "count": ie.get("playlist_count") or 0,
        "url": url,
        "site": errors.site_name(url) or str(ie.get("extractor_key") or ""),
        "is_channel": bool(_CHANNEL_PATH.search(urlsplit(url).path or "")) if url else False,
        "thumbnail": (thumbs[-1].get("url", "") if thumbs else "") or ie.get("thumbnail") or "",
    }


_UPCOMING = ("premieres in", "will begin in", "live event will begin", "is_upcoming",
             "not currently live", "starts in", "scheduled")


def _wait_for_release(jid: str, url: str, options: dict) -> None:
    """Wait for a scheduled premiere to be published, checking the job's
    cancel flag every second. yt-dlp's own wait sleeps inside extract_info,
    where Cancel cannot reach it, so a cancelled job held a worker for hours."""
    try:
        minutes = (float(options.get("wait_minutes") or 0)
                   or float(options.get("wait_hours") or 0) * 60 or 360.0)
    except (TypeError, ValueError):
        minutes = 360.0
    give_up = time.time() + minutes * 60
    while True:
        jobs.raise_if_cancelled(jid)
        status, release_at, title = "", None, ""
        opts = base_opts()
        opts.update({"skip_download": True, "ignore_no_formats_error": True,
                     "noplaylist": True})
        try:
            with session(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False) or {}
            status = str(info.get("live_status") or "")
            release_at = info.get("release_timestamp")
            title = info.get("title") or ""
        except Exception as exc:                 # noqa: BLE001 - "not yet" is an answer
            if not any(n in str(exc).lower() for n in _UPCOMING):
                break                            # the download reports the real problem
            status = "is_upcoming"
        if status not in ("is_upcoming", "is_live"):
            break
        now = time.time()
        if now >= give_up:
            raise errors.AppError("live_not_live", f"{url} was still not published after "
                                  f"{minutes:g} minutes of waiting ({status})",
                                  Channel=title or "It", title=title, gave_up_at=give_up,
                                  release_timestamp=release_at, live_status=status)
        wait = 60.0
        if release_at and release_at > now:
            wait = min(max(15.0, release_at - now), 300.0)
        next_at = min(now + wait, give_up)
        fields: dict[str, Any] = {
            "stage": "Waiting for the premiere" if status == "is_upcoming"
            else "Waiting for the premiere to end",
            "indeterminate": True, "next_check_at": next_at, "give_up_at": give_up}
        if title:
            fields["title"] = title
        jobs.update(jid, **fields)
        while time.time() < next_at:
            jobs.raise_if_cancelled(jid)
            time.sleep(1)
    jobs.update(jid, next_check_at=None, give_up_at=None)


def run_download(jid: str, url: str, options: dict) -> dict:
    """Worker body for a download job (see CONTRACT.md for the result)."""
    options = dict(options or {})
    cfg = config.get()
    outdir = str(cfg["download_dir"])            # never from the request
    Path(outdir).mkdir(parents=True, exist_ok=True)
    staging = None
    if cfg.get("use_temp_dir", True):
        _prune_staging()
        staging = claim_staging(job_temp_dir(url, options), jid)
    try:
        return _run_download(jid, url, options, cfg, outdir, staging)
    finally:
        release_staging(staging)


def _run_download(jid: str, url: str, options: dict, cfg: dict, outdir: str,
                  staging: str | None) -> dict:
    if _flag(options.get("wait_for_live")) or _flag(options.get("wait_for_video")):
        _wait_for_release(jid, url, options)

    opts = build_download_opts(options, outdir, staging)
    run = _Run(jid, url, options, opts, outdir)
    opts["logger"] = _Log(run)
    opts["match_filter"] = run.match_filter
    opts["progress_hooks"] = [run.progress]
    opts["postprocessor_hooks"] = [run.pp_hook]
    jobs.update(jid, stage="Getting video details and comments…" if opts.get("getcomments")
                else "Getting video details…", indeterminate=True)

    info = None
    outcome: BaseException | None = None
    try:
        with session(opts) as ydl:
            run.wrap_archive(ydl)
            _make_pps(ydl, run)
            ie = _resolve(ydl, url)
            if ie and ie.get("_type") in ("playlist", "multi_video"):
                # Now known to be a playlist: its own folder and numbering,
                # and one broken item no longer stops the rest.
                run.is_playlist = True
                ydl.params["outtmpl"].update(run.playlist_opts["outtmpl"])
                ydl.params["ignoreerrors"] = run.playlist_opts["ignoreerrors"]
                run.meta = _playlist_meta(ie)
            elif ie:
                run.meta = {k: v for k, v in summarize(ie).items() if k != "entries"}
            if run.meta:
                # The card shows what the link is right away, even when the
                # item is then skipped or fails.
                jobs.update(jid, **{k: v for k, v in (
                    ("title", run.meta.get("title")), ("uploader", run.meta.get("uploader")),
                    ("thumbnail", run.meta.get("thumbnail"))) if v})
            for attempt in range(3):
                if not ie:
                    break
                try:
                    info = ydl.process_ie_result(ie, download=True)
                    break
                except ReExtractInfo:
                    # A throttled download asks for fresh links; extract_info
                    # would retry by itself, process_ie_result does not.
                    if attempt == 2:
                        raise
                    ie = _resolve(ydl, url)
    except MaxDownloadsReached:
        run.stop_reason = "limit"
    except ExistingVideoReached:
        run.stop_reason = "up_to_date"
    except BaseException as exc:                 # noqa: BLE001 - sorted out below
        outcome = exc

    if outcome is not None and (isinstance(outcome, jobs.Cancelled) or run.cancelled()):
        run.cleanup_partials()
        raise jobs.Cancelled() from None

    if outcome is None and run.stop_reason is None and run.is_playlist and \
            playlist_mode(options) == "first" and run.saved and \
            not (run.archived or run.failed or run.rejects):
        # 'First 10' of a longer playlist stopped at the user's own limit (D-04).
        # With skipped or failed items the card counts those instead.
        try:
            first = max(1, int(float(options.get("playlist_first") or 10)))
        except (TypeError, ValueError):
            first = 10
        if int(run.meta.get("count") or 0) > first:
            run.stop_reason = "limit"

    if info is not None and not run.is_playlist:
        run.meta = {k: v for k, v in summarize(info).items() if k != "entries"}
        for got in info.get("requested_downloads") or []:
            if got.get("filepath") and os.path.isfile(got["filepath"]):
                run.files.append((got["filepath"], "", ""))
                run.saved.add(run._key(info))
    elif info is not None:
        run.meta["count"] = run.meta.get("count") or run.count
        for got in info.get("requested_downloads") or []:     # the joined file
            if got.get("filepath"):
                run.files.append((got["filepath"], "", "All videos joined"))
                # The joined items are gone, and so may be their folder's contents.
                _drop_empty_folders([p for p, _, _ in run.files if not os.path.exists(p)], outdir)
    attached = run.attach()

    if outcome is not None:
        if run.saved and "concatenat" in str(outcome).lower():
            run.note("Couldn't join the videos into one file, because they differ or some "
                     "failed. The separate files are kept.")
        else:
            run.cleanup_partials()
            raise outcome
    if run.failed:
        run.cleanup_partials()
    if run.reused and run.reused >= len(run.saved):
        run.note("Already in your folder, so it wasn't downloaded again." if run.reused == 1
                 else f"All {run.reused} were already in your folder, so nothing was "
                 "downloaded again.")
    if staging and run.saved and not run.failed:
        shutil.rmtree(staging, ignore_errors=True)
    if not run.saved and not attached:
        run.cleanup_partials()
        if staging:
            with contextlib.suppress(OSError):
                os.rmdir(staging)                # only when it is empty
        run.nothing_saved()
    return run.result(info)
