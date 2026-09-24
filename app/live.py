"""Live stream recording.

yt-dlp can download a live stream, but it hands the job to ffmpeg internally and
gives nothing back: no progress callbacks fire, and there is no way to stop the
recording and keep what you have. For live that is the whole feature, so this
module drives ffmpeg itself.

yt-dlp still does the hard part, resolving the stream manifest and the headers
needed to fetch it, which is why this works on every site it supports, not just
YouTube. ffmpeg then runs with `-c copy`, so nothing is re-encoded: video and
audio are muxed together into one file as they arrive.

Recording into MPEG-TS matters. It is a stream container with no index to write
at the end, so a recording that is stopped, crashes, or loses the network is
still a complete, playable file up to that point. It is rewrapped into the
container the user picked once the recording is over.

A live stream is not one long download. HLS fetches a fresh playlist and new
segments every few seconds, and one failed fetch ends ffmpeg with exit code 0.
So a recording is a loop of ffmpeg sessions: when a session ends on its own, the
stream is resolved again (its signed URLs expire too) and, while it is still
live, recording carries on into the next numbered part file.
"""
from __future__ import annotations

import contextlib
import math
import os
import re
import subprocess
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlparse

from yt_dlp import YoutubeDL

from . import config, cookies, errors, jobs, media

# Input options for HTTP sources. They let ffmpeg ride out a short drop inside
# one session; anything longer is handled by the session loop in _Recorder.
RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "5xx",
             "-reconnect_delay_max", "30", "-rw_timeout", "15000000"]
# HLS only: retry a segment that failed instead of skipping it. ffmpeg refuses
# to start when an input option is not used by anything, so these go on m3u8
# inputs only, and RECONNECT on http(s) inputs only.
HLS_OPTS = ["-seg_max_retry", "10"]

QUALITY_SELECTORS = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",
    # Sound only: a real audio rendition when the site has one (YouTube and
    # Twitch do). Otherwise a small muxed variant, whose picture is dropped:
    # it carries the same sound as the 1080p one at a fraction of the traffic,
    # which adds up over a night of recording.
    "audio": "ba/b[height<=480]/b",
}

STOP_GRACE = 20.0          # seconds ffmpeg gets to close the file after "q"
STALL_SECONDS = 90.0       # no new data for this long ends the session so it can reconnect
RECONNECT_WINDOW = 600.0   # how long a dropped stream is retried before giving up
OFFLINE_GRACE = 120.0      # a channel that drops offline after an error often comes back
SHORT_SESSION = 5.0        # a session shorter than this made no real progress
MAX_SHORT_SESSIONS = 3
MIN_SPLIT_SECONDS = 60     # parts are cut on keyframes; shorter parts are pointless
LONG_POLL = 600.0          # longest single sleep while waiting for a scheduled start
FASTSTART_MAX = 1 << 30    # moving the MP4 index rewrites the file; skip it for big ones
MAX_MINUTES = 31 * 24 * 60 # longest "stop after" or "new file every" taken as typed
EMPTY_BYTES = 64 * 1024    # a session file this small with no media time holds nothing

# yt-dlp's answers for "this exists but is not live yet". Everything here means
# keep waiting, not failure.
_NOT_LIVE_YET = re.compile(
    r"not currently live|is not live|isn't live|not live (?:yet|right now|now)|offline|"
    r"will begin|premiere|upcoming|scheduled|starts in|starting soon|"
    r"has not (?:started|begun)|hasn't (?:started|begun)|"
    r"waiting for (?:the )?(?:stream|broadcast|host|streamer)", re.I)
_OFFLINE = re.compile(r"not currently live|offline|isn't live|is not live|not live (?:yet|right now|now)", re.I)
_UPCOMING = re.compile(r"will begin|premiere|upcoming|scheduled|starts in|starting soon", re.I)
_NO_FORMATS = re.compile(r"requested format is not available|no video formats found", re.I)
# Worth retrying while waiting or reconnecting: the network, not the link.
_TRANSIENT = re.compile(
    r"timed? ?out|getaddrinfo|name resolution|temporar|connection (?:reset|refused|aborted|closed)|"
    r"unreachable|network|http error 5\d\d|http error 429|too many requests|"
    r"remote end closed|eof occurred|ssl|winerror 100[5-6]\d|errno 11001|errno -?138", re.I)
_ERROR_LINE = re.compile(r"error|failed|timed out|refused|reset by peer|unreachable", re.I)
_ENDED = {"not_live", "was_live", "post_live"}
_LIVE_STAMP = re.compile(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*$")
_PROGRESS = re.compile(r"^(\w+)=(.*)$")
_STREAM_URL = re.compile(r"(?:https?|rtmps?|rtsps?)://", re.I)
_STREAM = re.compile(r"Stream #\d+:\d+\S*: (Video|Audio|Data|Subtitle|Attachment): ([A-Za-z0-9_]+)")
_DURATION = re.compile(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)")

# Containers each final format can hold with a plain stream copy.
_MP4_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video"}
_MP4_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "flac", "alac"}
# MPEG-TS cannot carry these (VP9 is stored as unplayable private data, so a
# DASH source would come out as sound only). They are recorded into Matroska.
_TS_UNSAFE = ("vp8", "vp9", "vp09", "av01", "av1", "vorbis", "theora", "flac", "alac", "pcm")


class NotLiveYet(errors.AppError):
    """The link is a channel that is offline, or a stream that has not started.

    Kept apart from real failures so the check can say "isn't live right now"
    and waiting knows to keep going. It is also a live_not_live AppError with
    the channel, title and start time as params, so any caller that lets it
    escape still shows "NASA isn't live right now", never yt-dlp's text."""

    def __init__(self, reason: str, meta: dict | None = None, upcoming: bool = False):
        self.reason = reason
        self.meta = meta or {}
        self.upcoming = upcoming
        params = {k: v for k, v in self.meta.items()
                  if k in ("title", "uploader", "thumbnail", "release_timestamp")
                  and v not in (None, "")}
        params["live_status"] = "is_upcoming" if upcoming else "offline"
        if self.meta.get("uploader"):
            params["Channel"] = self.meta["uploader"]
        super().__init__("live_not_live", reason, **params)


# --------------------------------------------------------------------- resolve

def resolve(url: str, quality: str = "best", audio_only: bool = False, *,
            details: bool = True) -> dict:
    """Ask yt-dlp for the stream URLs and headers. Works for any supported site.

    Raises NotLiveYet for an offline channel or a stream that has not started.
    With details, a scheduled stream costs one more request to learn its start
    time; the waiting loop turns that off for most of its polls."""
    url = live_url(url)
    selector = QUALITY_SELECTORS.get("audio" if audio_only else str(quality or "best"),
                                     QUALITY_SELECTORS["best"])
    try:
        with _ydl(_opts(format=selector)) as ydl:
            info = _first(ydl.extract_info(url, download=False))
            if info.get("live_status") == "is_upcoming":
                raise NotLiveYet("This stream hasn't started yet.", _meta_of(info), upcoming=True)
            return _describe(info, url, audio_only, ydl)
    except NotLiveYet:
        raise
    except Exception as exc:
        nl = _not_live_from(url, exc, details)
        if nl:
            raise nl from exc
        raise


def _describe(info: dict, url: str, audio_only: bool, ydl) -> dict:
    entries = info.get("requested_formats") or [info]
    video = next((f for f in entries if _has_video(f)), None)
    audio = next((f for f in entries if f is not video and _has_audio(f)), None)
    single = bool(video and len(entries) == 1 and _has_audio(video))

    if audio_only:
        video = None
        audio = audio or (entries[0] if entries else None)
        single = False
    elif video and not audio and not single:
        # The video rendition carries no sound and nothing was paired with it.
        # Resolve the audio track separately so the recording is never silent.
        audio = _best_audio(url)

    if not (video or audio) or not (video or audio).get("url"):
        raise RuntimeError("No playable stream found at that link.")
    for f in (video, audio):
        # ffmpeg opens whatever it is given, without yt-dlp's own guards: a
        # page must not be able to point it at local files or ffmpeg's
        # special protocols (file:, concat:, subfile:...).
        if f and not _STREAM_URL.match(str(f.get("url") or "")):
            raise RuntimeError("No playable stream found at that link.")

    meta = _summary(info)
    return {
        "is_live": bool(info.get("is_live")),
        "was_live": bool(info.get("was_live")),
        "live_status": info.get("live_status") or "",
        "is_upcoming": False,
        "title": meta["title"],
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "release_timestamp": info.get("release_timestamp"),
        "duration": info.get("duration") or 0,
        "meta": meta,
        "video": _target(video, ydl),
        "audio": _target(audio, ydl) if (audio and audio is not video) else None,
        "single": single,
    }


def _summary(info: dict) -> dict:
    """The job result's meta for a stream: what a card or the history needs.
    Built here rather than borrowed from the download engine, whose summary
    is shaped for finished videos (views, captions, heights)."""
    return {
        "kind": "live",
        "id": info.get("id") or "",
        "title": clean_title(info.get("title")) or "Live recording",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "url": info.get("webpage_url") or info.get("original_url") or "",
        "extractor": info.get("extractor_key") or "",
        "is_live": bool(info.get("is_live")),
        "live_status": info.get("live_status") or "",
        "release_timestamp": info.get("release_timestamp"),
        "description": (info.get("description") or "")[:600],
    }


_YT_CHANNEL_ROOT = re.compile(r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")


def live_url(url: str) -> str:
    """A bare YouTube channel link means its live stream. Read literally it is
    the channel's home page, whose first entry is an old upload."""
    url = (url or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = (p.hostname or "").lower()
    if (host == "youtube.com" or host.endswith(".youtube.com")) and _YT_CHANNEL_ROOT.match(p.path or ""):
        return p._replace(path=p.path.rstrip("/") + "/live").geturl()
    return url


def _not_live_from(url: str, exc: BaseException, details: bool) -> NotLiveYet | None:
    """Tell "not live yet" apart from a real failure.

    An offline channel says so plainly. A scheduled YouTube stream instead fails
    with its own reason ("This live event will begin in 3 hours") or with "no
    formats"; a second look that tolerates missing formats reads the schedule."""
    msg = _plain(str(exc))
    if _OFFLINE.search(msg):
        name = (_channel_name(url) if details else "") or _channel_from_url(url)
        return NotLiveYet(msg, {"uploader": name}, upcoming=False)
    looks = bool(_NOT_LIVE_YET.search(msg))
    if not looks and not _NO_FORMATS.search(msg):
        return None
    # "No formats" alone cannot tell a scheduled stream from a broken link, so
    # it always gets the second look, even on a quick poll: otherwise a wait
    # would fail on its second check whenever the site gave no reason.
    meta = _upcoming_meta(url) if (details or not looks) else {}
    upcoming = meta.get("live_status") == "is_upcoming" or bool(_UPCOMING.search(msg))
    if not looks and not upcoming:
        return None                   # a genuine "no formats" failure
    return NotLiveYet(msg, meta or {"uploader": _channel_from_url(url)}, upcoming=upcoming)


_YT_CHANNEL = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)")


def _channel_name(url: str) -> str:
    """An offline YouTube channel's display name ("Blender", where the link
    only says @BlenderOfficial or a UC... id). yt-dlp's "not currently live"
    carries no metadata, so this costs one quick look at the channel page.
    Anything that goes wrong means no name, never a failure."""
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    m = _YT_CHANNEL.match(p.path or "")
    if not m or not (host == "youtube.com" or host.endswith(".youtube.com")):
        return ""
    try:
        with _ydl(_opts(extract_flat=True, playlist_items="0-0")) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/{m.group(1)}", download=False) or {}
    except Exception:
        return ""
    return str(info.get("channel") or info.get("uploader") or "")


def _upcoming_meta(url: str) -> dict:
    try:
        with _ydl(_opts(ignore_no_formats_error=True, format="b/bv*/ba")) as ydl:
            info = _first(ydl.extract_info(url, download=False))
    except Exception:
        return {}
    return _meta_of(info)


def _meta_of(info: dict) -> dict:
    return {"title": clean_title(info.get("title")),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "thumbnail": info.get("thumbnail") or "",
            "release_timestamp": info.get("release_timestamp"),
            "live_status": info.get("live_status") or ""}


class _Silent:
    """yt-dlp prints "ERROR: ... not currently live" even when quiet. Waiting
    asks every minute or two for hours, and each answer is handled here, so
    none of it belongs in the log."""

    def debug(self, msg):
        pass

    info = warning = error = debug


def _opts(**extra) -> dict:
    opts = media.base_opts()
    # A channel's streams tab is a playlist; only its first entry can be live,
    # and fetching the rest would take minutes.
    opts.update({"noplaylist": True, "skip_download": True, "playlist_items": "1",
                 "lazy_playlist": True, "quiet": True, "no_warnings": True,
                 "logger": _Silent()})
    opts.update(extra)
    return opts


@contextlib.contextmanager
def _ydl(opts: dict):
    """YoutubeDL that can never rewrite the user's cookies file.

    yt-dlp saves the whole jar back to its cookiefile on close, without locking.
    Waiting polls every minute or so while downloads run beside it, and two
    writers at once leave the file truncated. media.base_opts() (through
    _opts) already gives every call a private copy; it is handed back to the
    download engine afterwards, or every poll of a long wait would leave one
    more copy of the user's cookies on disk. Any other cookie file is copied
    here first (into the per-process folder the startup clean-up sweeps), so
    the user's own file is never handed to yt-dlp."""
    handed = opts
    copy = None
    src = opts.get("cookiefile")
    if src and not _engine_copy(src):
        try:
            copy = cookies.private_copy(src)
            opts = dict(opts, cookiefile=copy)
        except OSError:
            opts = {k: v for k, v in opts.items() if k != "cookiefile"}
    try:
        with YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if copy:
            cookies.discard_copy(copy)
        release = getattr(media, "release", None)
        if callable(release):
            with contextlib.suppress(Exception):
                release(handed)


def _engine_copy(path: str) -> bool:
    """Is this cookie file one of the download engine's private copies?"""
    folder = getattr(media, "_COOKIE_DIR", None)
    if folder is None:
        return False
    here = os.path.normcase(os.path.dirname(os.path.abspath(str(path))))
    return here == os.path.normcase(os.path.abspath(str(folder)))


def _first(info: dict | None) -> dict:
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Nothing to record at that link.")
        return entries[0]
    return info or {}


def _has_video(fmt: dict) -> bool:
    """A bare HLS link says nothing about its codecs. That may well carry a
    picture, so it counts as video: the recording then maps its video and
    sound as optional, and a sound-only stream still records fine. Counting
    it as sound only dropped the picture of every such stream."""
    if fmt.get("height") or fmt.get("width"):
        return True
    vcodec = fmt.get("vcodec")
    if vcodec is None:
        return not fmt.get("acodec")
    return vcodec != "none"


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
    try:
        with _ydl(_opts(format="ba/b")) as ydl:
            info = _first(ydl.extract_info(url, download=False))
            picked = (info.get("requested_formats") or [info])[0]
            if not picked.get("url"):
                return None
            # Cookies need the live YoutubeDL, so attach them here.
            return dict(picked, _cookies=_cookie_blob(ydl, picked.get("url", "")))
    except Exception:
        return None


def _target(fmt: dict | None, ydl=None) -> dict | None:
    if not fmt:
        return None
    url = fmt.get("url", "")
    cookies = fmt.get("_cookies")
    if cookies is None:
        cookies = _cookie_blob(ydl, url)
    return {"url": url, "headers": fmt.get("http_headers") or {},
            "ext": fmt.get("ext", ""), "protocol": fmt.get("protocol", ""),
            "height": fmt.get("height") or 0, "format_id": fmt.get("format_id", ""),
            "vcodec": fmt.get("vcodec") or "", "acodec": fmt.get("acodec") or "",
            "cookies": cookies}


def _cookie_blob(ydl, url: str) -> str:
    """Cookies for ffmpeg, in the Set-Cookie form its http reader expects.
    yt-dlp's own ffmpeg downloader passes them the same way."""
    if not ydl or not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        cookies = ydl.cookiejar.get_cookies_for_url(url)
    except Exception:
        return ""
    return "".join(f"{c.name}={c.value}; path={c.path}; domain={c.domain};\r\n" for c in cookies)


# ----------------------------------------------------------------------- check

def check(url: str, quality: str = "best", audio_only: bool = False) -> dict:
    """What the Live tab shows before recording: live now, offline or scheduled,
    or a regular video. An offline channel is an answer, not an error; only a
    real failure (bad link, private, network) raises."""
    site = errors.site_name(url)
    try:
        t = resolve(url, quality, False)
    except NotLiveYet as nl:
        m = nl.meta
        return {"is_live": False, "offline": True, "upcoming": nl.upcoming, "regular": False,
                "live_status": "is_upcoming" if nl.upcoming else "offline",
                "title": m.get("title") or "",
                "uploader": m.get("uploader") or _channel_from_url(url),
                "thumbnail": m.get("thumbnail") or "",
                "height": 0, "has_video": False, "has_audio": False,
                "release_timestamp": m.get("release_timestamp"),
                "site": site, "reason": nl.reason}
    regular = _is_regular(t)
    return {"is_live": not regular, "offline": False, "upcoming": False, "regular": regular,
            "live_status": t["live_status"],
            "title": t["title"], "uploader": t["uploader"], "thumbnail": t["thumbnail"],
            "height": 0 if audio_only else (t["video"] or {}).get("height", 0),
            "has_video": bool(t["video"]), "has_audio": bool(t["audio"] or t["single"]),
            "release_timestamp": t["release_timestamp"], "site": site, "reason": ""}


def _is_regular(target: dict) -> bool:
    """A finished video rather than a stream. Sites that say nothing about live
    status (radio, direct stream links) count as streams, unless they report a
    length, which live streams do not have."""
    if target.get("is_live"):
        return False
    status = target.get("live_status") or ""
    if status in _ENDED:
        return True
    if status in ("is_live", "is_upcoming"):
        return False
    return bool(target.get("duration"))


def clean_title(title: str | None) -> str:
    """yt-dlp appends ' YYYY-MM-DD HH:MM' to every live title. It is noise on a
    card, and the file name carries its own, more precise, time."""
    title = (title or "").strip()
    cleaned = _LIVE_STAMP.sub("", title).strip()
    return cleaned or title


def _channel_from_url(url: str) -> str:
    """Best guess at a channel name when the site gives no metadata, which is
    what an offline channel returns: youtube.com/@NASA/live -> NASA."""
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return ""
    parts = [unquote(s) for s in p.path.split("/") if s]
    for s in parts:
        if s.startswith("@") and len(s) > 1:
            return s[1:]
    host = (p.hostname or "").lower()
    if parts and any(h in host for h in ("twitch.tv", "kick.com")) \
            and parts[0].lower() not in ("videos", "directory", "search", "category", "categories"):
        return parts[0]
    return ""


# ------------------------------------------------------------------------ wait

def wait_until_live(url: str, jid: str | None, quality: str = "best", audio_only: bool = False,
                    deadline: float | None = None) -> dict:
    """Poll an offline channel or a scheduled stream until it goes live, then
    return the resolved stream (with the user's quality and sound-only choice).

    A YouTube stream scheduled for tonight fails with "This live event will
    begin in 3 hours", which is the reason to wait, not an error. Every
    not-live-yet answer and every network hiccup is retried until the deadline;
    anything else (removed, private, bad link) fails at once. When the start
    time is known, it sleeps until a minute before it instead of polling.

    deadline is in epoch seconds."""
    deadline = float(deadline) if deadline else time.time() + 180 * 60
    if jid:
        jobs.update(jid, live_phase="waiting", give_up_at=deadline, next_check_at=None,
                    stage="Not live yet", indeterminate=True, progress=0.0)
    delay, attempt, release, name = 15.0, 0, None, ""
    while True:
        if jid:
            jobs.raise_if_cancelled(jid)
        # Most polls skip the extra lookups (start time, channel name); a
        # scheduled start can move, so every tenth one looks again.
        details = attempt % 10 == 0
        try:
            return resolve(url, quality, audio_only, details=details)
        except NotLiveYet as nl:
            reason = nl.reason
            release = nl.meta.get("release_timestamp") or release
            if nl.meta.get("uploader") and (details or not name):
                name = nl.meta["uploader"]
            if jid and details:
                _hints(jid, nl.meta)
        except Exception as exc:
            if not _TRANSIENT.search(str(exc)):
                raise
            reason = _plain(str(exc))
        attempt += 1

        now = time.time()
        if now >= deadline:
            raise _not_live_error(
                f"Stopped waiting at {time.strftime('%H:%M')}. Last answer: {reason}",
                name or _channel_from_url(url), uploader=name, release_timestamp=release,
                live_status="is_upcoming" if release else "offline",
                # The user did ask to wait: the card should say it gave up,
                # not suggest turning waiting on.
                gave_up_at=deadline)
        if release and release - 60 > now + delay:
            nxt, delay = min(release - 60, now + LONG_POLL), 15.0
        else:
            nxt, delay = now + delay, min(delay * 1.5, 90.0)
        nxt = min(nxt, deadline)
        if jid:
            jobs.update(jid, next_check_at=nxt, stage=_waiting_stage(release), stage_detail=reason)
        _sleep_until(nxt, jid)


def _waiting_stage(release) -> str:
    if release and release > time.time():
        return f"Not live yet · scheduled for {time.strftime('%H:%M', time.localtime(release))}"
    return "Not live yet"


def _sleep_until(when: float, jid: str | None) -> None:
    """Sleep in short slices so Stop is noticed within half a second."""
    while True:
        if jid:
            jobs.raise_if_cancelled(jid)
        left = when - time.time()
        if left <= 0:
            return
        time.sleep(min(0.5, left))


def _pause(seconds: float, jid: str) -> bool:
    """Sleep unless the user stops the job first; False means they did."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if jobs.cancelled(jid):
            return False
        time.sleep(min(0.5, max(end - time.monotonic(), 0)))
    return not jobs.cancelled(jid)


# --------------------------------------------------------------------- options

def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _minutes(value) -> float | None:
    """Minutes as typed, in seconds. Accepts fractions and a decimal comma;
    blank, zero, negative or nonsense means "not set"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        m = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(m) or m <= 0:
        return None
    # Capped so a typo like 1e9 still gives ffmpeg a time it can parse.
    return min(m, MAX_MINUTES) * 60


def limit_seconds(o: dict) -> int | None:
    s = _minutes(o.get("max_minutes"))
    return max(1, int(round(s))) if s else None


def split_seconds(o: dict) -> int | None:
    s = _minutes(o.get("split_minutes"))
    return max(MIN_SPLIT_SECONDS, int(round(s))) if s else None


def wait_seconds(o: dict) -> float:
    s = _minutes(o.get("wait_minutes"))
    return min(max(s or 180 * 60, 60.0), 14 * 86400.0)


def _container(value, audio_only: bool) -> str:
    """The final container the user asked for. The UI offers mp4/mkv/ts for
    video and m4a/ts for sound only; older requests send mp4 for both."""
    value = str(value or "").lower().strip(". ")
    if value == "ts":
        return "ts"
    if audio_only:
        return "mka" if value in ("mkv", "mka") else "m4a"
    return "mkv" if value in ("mkv", "mka") else "mp4"


def _codec(name: str) -> str:
    n = (name or "").lower()
    for prefix, family in (("avc", "h264"), ("h264", "h264"), ("hev", "hevc"), ("hvc", "hevc"),
                           ("h265", "hevc"), ("vp09", "vp9"), ("vp9", "vp9"), ("vp8", "vp8"),
                           ("av01", "av1"), ("av1", "av1"), ("mp4a", "aac"), ("aac", "aac"),
                           ("mp3", "mp3"), ("opus", "opus"), ("vorbis", "vorbis"),
                           ("flac", "flac"), ("ac-3", "ac3"), ("ac3", "ac3"), ("ec-3", "eac3"),
                           ("eac3", "eac3")):
        if n.startswith(prefix):
            return family
    return n


def record_ext(target: dict) -> str:
    """Record into MPEG-TS unless a known codec cannot live in it."""
    codecs = []
    for key in ("video", "audio"):
        t = target.get(key) or {}
        codecs += [_codec(t.get("vcodec", "")), _codec(t.get("acodec", ""))]
    if any(c and c != "none" and c.startswith(_TS_UNSAFE) for c in codecs):
        return "mkv"
    return "ts"


def final_ext(streams: list[tuple[str, str]], container: str, src_ext: str) -> str:
    """Pick the extension the recording ends up with, from what it really holds.

    MP4 is refused for codecs it cannot carry, and sound only goes to .m4a only
    when the sound is AAC; MP3 radio becomes .mp3, anything else .mka. TS stays
    TS (unless it could not be recorded as TS in the first place)."""
    video = [c for kind, c in streams if kind == "Video"]
    audio = [c for kind, c in streams if kind == "Audio"]
    if container == "ts" or not (video or audio):
        return src_ext
    if not video:
        if container in ("mkv", "mka"):
            return "mka"
        if all(a == "aac" for a in audio):
            return "m4a"
        if len(audio) == 1 and audio[0] == "mp3":
            return "mp3"
        return "mka"
    if container == "mkv":
        return "mkv"
    if all(v in _MP4_VIDEO for v in video) and all(a in _MP4_AUDIO for a in audio):
        return "mp4"
    return "mkv"


# ----------------------------------------------------------------- ffmpeg glue

def _ffmpeg() -> str:
    d = config.ffmpeg_dir()
    return str(Path(d) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")) if d else "ffmpeg"


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


def _input_args(t: dict) -> list[str]:
    url = t.get("url") or ""
    args: list[str] = []
    if url.lower().startswith(("http://", "https://")):
        args += _header_blob(t.get("headers") or {})
        if t.get("cookies"):
            args += ["-cookies", t["cookies"]]
        args += RECONNECT
        if str(t.get("protocol") or "").startswith("m3u8"):
            args += HLS_OPTS
    return args + ["-i", url]


def _num(value) -> float:
    """ffmpeg's progress fields read "N/A" whenever it does not know yet (the
    segment muxer never knows total_size). Anything unparsable is 0."""
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) and n > 0 else 0.0


def _fmt_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def build_command(target: dict, out_path: Path, o: dict, *, part_start: int = 1,
                  remaining: float | None = None) -> list[str]:
    """The ffmpeg command for one recording session.

    out_path names the file (or, when splitting, the stem of the numbered
    parts); its extension picks the muxer. remaining overrides the time limit
    for a session that continues an earlier one."""
    out_path = Path(out_path)
    cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "warning",
           "-n",                                   # never overwrite anything
           "-progress", "pipe:1", "-stats_period", "1"]

    video, audio = target.get("video"), target.get("audio")
    if video:
        cmd += _input_args(video)
    if audio:
        cmd += _input_args(audio)

    if video and audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    elif video:
        # Explicit maps drop the timed-metadata data streams some HLS sources
        # carry; MP4 cannot hold them and the rewrap would fail.
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
    else:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c", "copy"]

    limit = remaining if remaining is not None else limit_seconds(o)
    if limit:
        cmd += ["-t", _fmt_seconds(max(float(limit), 1.0))]

    fmt = "matroska" if out_path.suffix.lower() == ".mkv" else "mpegts"
    split = split_seconds(o)
    if split:
        # The segment muxer reads the name as a printf pattern: a literal '%'
        # from a title ("100% Radio") must be doubled or nothing is recorded.
        stem = out_path.stem.replace("%", "%%")
        pattern = out_path.with_name(f"{stem} part%03d{out_path.suffix}")
        cmd += ["-f", "segment", "-segment_time", str(split),
                "-segment_start_number", str(part_start),
                "-reset_timestamps", "1", "-segment_format", fmt, str(pattern)]
    else:
        cmd += ["-f", fmt, str(out_path)]
    return cmd


def _proxy() -> str:
    proxy = str(config.get().get("proxy") or "").strip()
    if proxy and not re.match(r"[\da-zA-Z]+://", proxy):
        proxy = "http://" + proxy
    return proxy


def _ffmpeg_env() -> dict | None:
    """ffmpeg fetches the stream itself, so it needs the proxy too. It reads
    http_proxy from the environment, as yt-dlp's own ffmpeg downloader passes
    it, and uses it for https streams as well (through CONNECT). It only
    understands http:// proxies: a SOCKS proxy cannot be handed to it."""
    proxy = _proxy()
    if not proxy.lower().startswith("http://"):
        return None
    env = os.environ.copy()
    env["http_proxy"] = env["HTTP_PROXY"] = proxy
    return env


def _popen(cmd: list[str], **kw) -> subprocess.Popen:
    if os.name == "nt":
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc = subprocess.Popen(cmd, **kw)
    except FileNotFoundError as exc:
        raise errors.AppError("ffmpeg_missing", str(exc)) from exc
    _tie_to_app(proc)
    return proc


_JOB_HANDLE = None
_job_lock = threading.Lock()


def _tie_to_app(proc: subprocess.Popen) -> None:
    """Windows: put ffmpeg in a job object that is closed when this process
    ends. A crash or a killed app then takes the recording down with it instead
    of leaving an ffmpeg that records forever with nothing able to stop it."""
    global _JOB_HANDLE
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        with _job_lock:
            if _JOB_HANDLE is None:
                class Basic(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                                ("PerJobUserTimeLimit", ctypes.c_int64),
                                ("LimitFlags", wintypes.DWORD),
                                ("MinimumWorkingSetSize", ctypes.c_size_t),
                                ("MaximumWorkingSetSize", ctypes.c_size_t),
                                ("ActiveProcessLimit", wintypes.DWORD),
                                ("Affinity", ctypes.c_size_t),
                                ("PriorityClass", wintypes.DWORD),
                                ("SchedulingClass", wintypes.DWORD)]

                class Extended(ctypes.Structure):
                    _fields_ = [("BasicLimitInformation", Basic),
                                ("IoInfo", ctypes.c_uint64 * 6),
                                ("ProcessMemoryLimit", ctypes.c_size_t),
                                ("JobMemoryLimit", ctypes.c_size_t),
                                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                                ("PeakJobMemoryUsed", ctypes.c_size_t)]

                handle = k32.CreateJobObjectW(None, None)
                if not handle:
                    return
                info = Extended()
                info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
                if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info),
                                                   ctypes.sizeof(info)):
                    k32.CloseHandle(handle)
                    return
                _JOB_HANDLE = handle
            k32.AssignProcessToJobObject(_JOB_HANDLE, int(proc._handle))
    except Exception:
        pass            # best effort: recording works the same without it


def _run_ffmpeg(cmd: list[str], jid: str, on_tick, env: dict | None = None,
                stalled=None) -> tuple[int, str, str]:
    """Run one ffmpeg session and report its progress.

    Returns (exit code, the last lines ffmpeg logged, how it ended), where how is
    "exit" (ffmpeg stopped by itself), "user" (Stop was pressed) or "stalled"
    (no new data for too long, so the caller can reconnect).

    stderr is drained on its own thread, keeping only the tail: a long session
    logs enough warnings to fill the pipe and freeze ffmpeg otherwise. A 0.5 s
    timer thread watches for Stop and sends "q" even when no progress line is
    arriving, which is exactly when a stalled stream needs stopping. A tick that
    raises can never end the read loop: that silent early exit used to kill
    every split recording after about 25 seconds."""
    proc = _popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  encoding="utf-8", errors="replace", bufsize=1, env=env)
    tail: deque[str] = deque(maxlen=60)
    ended = threading.Event()
    state = {"how": "exit", "asked": 0.0}

    def drain() -> None:
        with contextlib.suppress(Exception):
            for line in proc.stderr:
                if line.strip():
                    tail.append(line.rstrip())

    def watch() -> None:
        while not ended.wait(0.5):
            if proc.poll() is not None:
                return
            if state["asked"]:
                if time.monotonic() - state["asked"] > STOP_GRACE:
                    with contextlib.suppress(Exception):
                        proc.kill()
                continue
            why = "user" if jobs.cancelled(jid) else ("stalled" if stalled and stalled() else "")
            if why:
                state["how"], state["asked"] = why, time.monotonic()
                try:
                    # "q" is ffmpeg's graceful quit: it closes the file properly.
                    proc.stdin.write("q")
                    proc.stdin.flush()
                except Exception:
                    with contextlib.suppress(Exception):
                        proc.kill()

    threads = [threading.Thread(target=drain, daemon=True, name="live-stderr"),
               threading.Thread(target=watch, daemon=True, name="live-watch")]
    for t in threads:
        t.start()

    stats: dict[str, str] = {}
    complained = False
    try:
        for line in proc.stdout:
            m = _PROGRESS.match(line.strip())
            if not m:
                continue
            stats[m.group(1)] = m.group(2)
            if m.group(1) == "progress":
                try:
                    on_tick(dict(stats))
                except Exception:
                    if not complained:
                        complained = True
                        with contextlib.suppress(Exception):
                            traceback.print_exc()
    finally:
        # stdout is closed, so ffmpeg is on its way out; the watcher still kills
        # it if a requested stop takes too long.
        try:
            proc.wait(timeout=STOP_GRACE + 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        ended.set()
        for t in threads:
            t.join(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()
    return proc.returncode or 0, "\n".join(tail), state["how"]


def _run_quiet(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    proc = _popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def probe(path: Path) -> tuple[list[tuple[str, str]], float]:
    """(kind, codec) of every stream in a file and its length in seconds (0
    when unknown), read from ffmpeg's own report so no ffprobe is needed."""
    try:
        res = _run_quiet([_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path)], timeout=60)
    except Exception:
        return [], 0.0
    text = res.stderr or ""
    streams = [(m.group(1), m.group(2).lower()) for m in _STREAM.finditer(text)]
    m = _DURATION.search(text)
    seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return streams, seconds


def probe_streams(path: Path) -> list[tuple[str, str]]:
    return probe(path)[0]


def remux(src: Path, container: str = "mp4", audio_only: bool = False) -> Path:
    """Rewrap a finished recording without re-encoding.

    Returns the new file, or src when it is already right or the rewrap fails;
    a recording is never lost, and a failed rewrap never leaves an empty or
    half-written file beside it. aac_adtstoasc is not forced: the MP4 muxer
    adds it by itself for AAC, and forcing it rejected MP3 and Opus sound."""
    src = Path(src)
    if not src.exists() or src.stat().st_size == 0:
        return src
    container = _container(container, audio_only)
    streams = probe_streams(src)
    ext = final_ext(streams, container, src.suffix[1:].lower())
    if ext == src.suffix[1:].lower():
        return src
    dest = _free_path(src.with_suffix("." + ext))
    size = src.stat().st_size
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(src),
           "-map", "0:v?", "-map", "0:a?", "-c", "copy"]
    if ext in ("mp4", "m4a") and size < FASTSTART_MAX:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(dest))
    ok = False
    try:
        res = _run_quiet(cmd, timeout=600 + size / 10_000_000)
        ok = res.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except Exception:
        ok = False
    if not ok:
        with contextlib.suppress(OSError):
            dest.unlink()
        return src
    with contextlib.suppress(OSError):
        src.unlink()
    return dest


def _free_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        alt = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not alt.exists():
            return alt
        n += 1


# ----------------------------------------------------------------- file names

_claimed: set[str] = set()
_claim_lock = threading.Lock()


def _safe_name(text: str, restricted: bool = False) -> str:
    if restricted:
        from yt_dlp.utils import sanitize_filename
        text = sanitize_filename(text, restricted=True)
    else:
        text = "".join("-" if (c in '<>:"/\\|?*' or ord(c) < 32) else c for c in text)
    return " ".join(text.split()).strip(" ._-")


def base_stem(title: str, when: float | None = None, restricted: bool = False,
              room: int = 100) -> str:
    """Title plus the start time to the second. The title's own YouTube stamp
    is minute-only, so two recordings started in the same minute collided.
    room caps the title part (see _title_room)."""
    name = _safe_name(clean_title(title), restricted)[:max(room, 1)].rstrip(" ._-") or \
        ("live_recording" if restricted else "Live recording")
    stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(when or time.time()))
    return f"{name}_{stamp.replace(' ', '_')}" if restricted else f"{name} {stamp}"


def _title_room(outdir: Path) -> int:
    """How much of the title fits in the file name. Windows paths stop at 260
    characters unless long paths are switched on, and past that Python cannot
    even see the file ffmpeg wrote, so the recording would be lost from the
    job. Room is left for the time, " part001 (2)" and the extension."""
    return max(20, min(100, 259 - len(str(outdir)) - 1 - 20 - 18))


def _stem_taken(outdir: Path, stem: str) -> bool:
    low = stem.lower()
    pat = re.compile(re.escape(low) + r"(?: part\d+)?(?: \(\d+\))?\.[a-z0-9]+$")
    try:
        with os.scandir(outdir) as it:
            return any(pat.fullmatch(e.name.lower()) for e in it)
    except OSError:
        return False


def claim_stem(outdir: Path, base: str) -> str:
    """Reserve a file stem nobody else uses, on disk or in a recording that is
    running right now. Two jobs for the same stream in the same second get
    'name' and 'name (2)' instead of writing into one file."""
    with _claim_lock:
        n = 1
        while True:
            stem = base if n == 1 else f"{base} ({n})"
            key = str(Path(outdir) / stem).lower()
            if key not in _claimed and not _stem_taken(Path(outdir), stem):
                _claimed.add(key)
                return stem
            n += 1


def release_stem(outdir: Path, stem: str) -> None:
    with _claim_lock:
        _claimed.discard(str(Path(outdir) / stem).lower())


# ------------------------------------------------------------------ job body

def run_live(jid: str, url: str, o: dict) -> dict:
    """Worker body for a live recording job."""
    o = dict(o or {})
    # Folders come from settings only, never from the request.
    outdir = Path(config.get()["download_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    quality = str(o.get("quality") or "best")
    audio_only = _flag(o.get("audio_only"))
    limit = limit_seconds(o)

    jobs.update(jid, stage="Checking the stream…", indeterminate=True, progress=0.0,
                rec_limit_seconds=limit)
    if _flag(o.get("wait_for_live")):
        target = wait_until_live(url, jid, quality, audio_only, time.time() + wait_seconds(o))
    else:
        try:
            target = resolve(url, quality, audio_only)
        except NotLiveYet as nl:
            m = nl.meta
            _hints(jid, m)
            raise _not_live_error(
                nl.reason, m.get("uploader") or _channel_from_url(url),
                title=m.get("title"), uploader=m.get("uploader"), thumbnail=m.get("thumbnail"),
                release_timestamp=m.get("release_timestamp"),
                live_status="is_upcoming" if nl.upcoming else "offline") from None
    # Stop pressed while the stream was being looked up: nothing was recorded.
    jobs.raise_if_cancelled(jid)
    _hints(jid, target)

    if _is_regular(target):
        # Links that are not live are refused rather than recorded: stream-
        # copying a finished video through the live path lost its picture
        # (VP9 cannot live in MPEG-TS). The card offers Download instead.
        raise _not_live_error("This is a regular video, not a live stream.",
                              target["uploader"], title=target["title"],
                              uploader=target["uploader"], thumbnail=target["thumbnail"],
                              live_status=target["live_status"] or "not_live")

    rec = _Recorder(jid, url, target, o, outdir)
    return rec.run()


def _hints(jid: str, meta: dict) -> None:
    fields = {k: meta.get(k) for k in ("title", "uploader", "thumbnail") if meta.get(k)}
    if fields:
        jobs.update(jid, **fields)


def _not_live_error(detail: str, name: str, **extra) -> Exception:
    """live_not_live, carrying what the card needs: the channel for the
    headline, plus title, thumbnail, uploader, live_status and start time so
    the UI can tell an offline channel from a regular video and offer the
    right next step."""
    params = {k: v for k, v in extra.items() if v not in (None, "")}
    if name:
        params["Channel"] = name
    return errors.AppError("live_not_live", detail, **params)


class _Recorder:
    """One recording: a loop of ffmpeg sessions writing numbered part files,
    then a rewrap of each part into the container the user picked."""

    def __init__(self, jid: str, url: str, target: dict, o: dict, outdir: Path):
        self.jid, self.url, self.target, self.o, self.outdir = jid, url, target, o, outdir
        self.quality = str(o.get("quality") or "best")
        self.audio_only = _flag(o.get("audio_only"))
        self.container = _container(o.get("container"), self.audio_only)
        self.limit = limit_seconds(o)
        self.split = split_seconds(o)
        self.ext = record_ext(target)
        restricted = bool(config.get().get("restrict_filenames"))
        self.stem = claim_stem(outdir, base_stem(target["title"], restricted=restricted,
                                                 room=_title_room(outdir)))

        self.files: list[Path] = []     # finished session output, in order
        self.done_seconds = 0.0         # recorded before the current session
        self.done_bytes = 0
        self.seconds = 0.0              # totals shown on the card
        self.bytes = 0
        self.sessions = 0
        self.reconnects = 0
        self.last_code = 0
        self.next_part = 1
        # per-session state
        self.cur_out: Path | None = None
        self.cur_part = 1
        self.closed_bytes = 0           # finished parts of the current session
        self.last_seen = time.monotonic()
        self.session_secs = 0.0
        self.session_bytes = 0
        self.shown = (0.0, 0)            # session clock and size last put on the card

    # -- naming ---------------------------------------------------------------
    def _part(self, n: int) -> Path:
        return self.outdir / f"{self.stem} part{n:03d}.{self.ext}"

    # -- main loop ------------------------------------------------------------
    def run(self) -> dict:
        end, fatal, tail, short, clean = "user", None, "", 0, True
        try:
            while True:
                if jobs.cancelled(self.jid):
                    end = "user"
                    break
                code, tail, how = self._session()
                self.last_code = code
                if how == "user" or jobs.cancelled(self.jid):
                    end = "user"
                    break
                if self.limit and self.done_seconds >= self.limit - 1.5:
                    end = "limit"
                    break
                fatal = _fatal(tail)
                if fatal:
                    end = "connection_lost"
                    break
                clean = how == "exit" and code == 0 and not _ERROR_LINE.search(tail)
                short = short + 1 if self.session_secs < SHORT_SESSION else 0
                if short >= MAX_SHORT_SESSIONS:
                    end = "stream_ended" if clean else "connection_lost"
                    break
                # A session that died at once (the site refusing the stream for
                # a moment) is retried after a short pause, not in a tight loop.
                if short and not _pause(3.0 * short, self.jid):
                    end = "user"
                    break
                nxt = self._reconnect(clean)
                if isinstance(nxt, str):
                    end = nxt
                    break
                self.target = nxt
                self.reconnects += 1
            return self._finish(end, fatal, tail)
        finally:
            release_stem(self.outdir, self.stem)

    def _session(self) -> tuple[int, str, str]:
        remaining = (self.limit - self.done_seconds) if self.limit else None
        plain = self.outdir / f"{self.stem}.{self.ext}"
        if self.split:
            out = plain
            self.cur_part = self.next_part
        elif not self.files:
            out = plain
        else:
            first = self.files[0]
            if len(self.files) == 1 and first.stem == self.stem:
                # A second session makes this a multi-part recording: number
                # the first file too, so the parts sort and read as a set.
                renamed = first.with_name(f"{self.stem} part001{first.suffix}")
                with contextlib.suppress(OSError):
                    first.rename(renamed)
                    self.files[0] = renamed
                self.next_part = max(self.next_part, 2)
            out = self._part(self.next_part)
        self.cur_out = out
        self.closed_bytes = 0
        self.session_secs = 0.0
        self.session_bytes = 0
        self.shown = (0.0, 0)
        self.last_seen = time.monotonic()

        cmd = build_command(self.target, out, self.o, part_start=self.next_part,
                            remaining=remaining)
        v = self.target.get("video") or {}
        a = self.target.get("audio") or v
        detail = " · ".join(x for x in (f"{v['height']}p" if v.get("height") else "",
                                        _codec(v.get("vcodec", "")) if v else "",
                                        _codec(a.get("acodec", "")) if a else "")
                            if x and x != "none")
        fields = dict(live_phase="recording", indeterminate=False, next_check_at=None,
                      give_up_at=None, rec_limit_seconds=self.limit,
                      rec_seconds=round(self.seconds, 1), rec_bytes=self.bytes,
                      parts=max(len(self.files) + 1, 1), speed="", eta="",
                      stage=_rec_stage(self.seconds, self.bytes, self.limit),
                      stage_detail=detail)
        if self.limit:
            fields["progress"] = min(self.seconds / self.limit, 0.999)
        if not jobs.cancelled(self.jid):
            jobs.update(self.jid, **fields)

        try:
            code, tail, how = _run_ffmpeg(cmd, self.jid, self._tick, env=_ffmpeg_env(),
                                          stalled=self._stalled)
        finally:
            self.sessions += 1
            self._collect()
        return code, tail, how

    def _stalled(self) -> bool:
        return time.monotonic() - self.last_seen > STALL_SECONDS

    def _tick(self, stats: dict) -> None:
        secs = _num(stats.get("out_time_us")) / 1_000_000
        size = int(_num(stats.get("total_size")))
        if self.split:
            # The segment muxer reports no size; read it from the parts on disk.
            # Only the newest part can still grow, so older ones are counted once.
            while self._part(self.cur_part + 1).exists():
                self.closed_bytes += _size_of(self._part(self.cur_part))
                self.cur_part += 1
            size = self.closed_bytes + _size_of(self._part(self.cur_part))
        elif not size and self.cur_out is not None:
            size = _size_of(self.cur_out)

        self.session_secs = max(self.session_secs, secs)
        self.session_bytes = max(self.session_bytes, size)
        # New media time is the sign of life. The size alone is not: ffmpeg
        # flushes its buffer when a dead session exits, which would look like
        # fresh data just as the stream is lost. Size counts only when the
        # stream has no clock at all.
        if self.session_secs > 0:
            grew = self.session_secs > self.shown[0] + 0.2
        else:
            grew = self.session_bytes > self.shown[1]
        if not grew:
            # Leave the job untouched so its 'updated' time ages and the card
            # can show that the stream has stalled.
            return
        self.shown = (self.session_secs, self.session_bytes)
        self.last_seen = time.monotonic()
        self.seconds = max(self.seconds, self.done_seconds + self.session_secs)
        self.bytes = max(self.bytes, self.done_bytes + self.session_bytes)
        parts = len(self.files) + (self.cur_part - self.next_part + 1 if self.split else 1)
        fields = dict(rec_seconds=round(self.seconds, 1), rec_bytes=self.bytes, parts=parts)
        if jobs.cancelled(self.jid):
            fields.update(live_phase="saving", stage=_saving_stage(self.seconds))
        else:
            fields["stage"] = _rec_stage(self.seconds, self.bytes, self.limit)
        if self.limit:
            fields["progress"] = min(self.seconds / self.limit, 0.999)
        jobs.update(self.jid, **fields)

    def _collect(self) -> None:
        """Gather what the session wrote. Empty files are deleted on the spot
        and their numbers used again, so the parts on disk read 1, 2, 3."""
        if self.split:
            found, n = [], self.next_part
            while True:
                p = self._part(n)
                if not p.exists():
                    # A gap is possible only at the very start; one look ahead.
                    if not self._part(n + 1).exists():
                        break
                else:
                    found.append(p)
                n += 1
        else:
            found = [self.cur_out] if self.cur_out and self.cur_out.exists() else []
        # A session that never reported any media time can still leave a
        # file: Matroska writes its header before the first packet. That is
        # not a recording, and keeping it would turn "nothing was recorded"
        # into a finished job with a useless part.
        hollow = self.session_secs <= 0
        for p in found:
            size = _size_of(p)
            if size > 0 and not (hollow and size < EMPTY_BYTES):
                self.files.append(p)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
        numbers = [int(m.group(1)) for p in self.files
                   if (m := re.search(r" part(\d+)$", p.stem))]
        if numbers:
            self.next_part = max(numbers) + 1
        self.done_seconds += self.session_secs
        self.done_bytes = sum(_size_of(p) for p in self.files)
        self.seconds = max(self.seconds, self.done_seconds)
        self.bytes = max(self.bytes, self.done_bytes)

    def _reconnect(self, clean: bool) -> dict | str:
        """The session ended by itself. Find out whether the stream is over or
        just dropped, and return a fresh target to continue with, or the reason
        to stop. The job is not touched while this runs, so the card shows the
        stall ("no data for N s") the whole time."""
        started = time.monotonic()
        offline_since = None
        # Short gaps: every second spent here is missing from the recording,
        # and only the stream's last few segments can be caught up on.
        delays = iter((2, 5, 10))
        while True:
            if jobs.cancelled(self.jid):
                return "user"
            try:
                t = resolve(self.url, self.quality, self.audio_only, details=False)
            except NotLiveYet:
                if clean:
                    return "stream_ended"
                offline_since = offline_since or time.monotonic()
                if time.monotonic() - offline_since >= OFFLINE_GRACE:
                    return "stream_ended"
            except Exception as exc:
                if clean and not _TRANSIENT.search(str(exc)):
                    return "stream_ended"      # e.g. made private once it ended
            else:
                if _is_regular(t) or t.get("live_status") in _ENDED:
                    return "stream_ended"
                if clean and not t.get("is_live"):
                    return "stream_ended"      # a direct link that simply finished
                if record_ext(t) != self.ext:
                    self.ext = record_ext(t)
                return t
            if time.monotonic() - started >= RECONNECT_WINDOW:
                return "connection_lost"
            if not _pause(next(delays, 15), self.jid):
                return "user"

    def _finish(self, end: str, fatal: str | None, tail: str) -> dict:
        jobs.update(self.jid, live_phase="saving", indeterminate=True, speed="", eta="",
                    stage=_saving_stage(self.seconds))
        if not self.files:
            # A full drive is reported as such even though the loop's end
            # reason is still its default: nobody pressed Stop.
            if fatal:
                raise errors.AppError(fatal, tail, folder=str(self.outdir))
            if end == "user":
                raise jobs.Cancelled()
            raise _nothing_recorded(self.last_code, tail)

        if len(self.files) == 1:
            # One file after all (a split time longer than the recording, or a
            # reconnect that brought nothing): it is not "part 1" of anything.
            only = self.files[0]
            plain = only.with_name(f"{self.stem}{only.suffix}")
            if only != plain and not plain.exists():
                with contextlib.suppress(OSError):
                    only.rename(plain)
                    self.files[0] = plain

        # A full disk cannot hold a rewrapped copy; keep the recording as is.
        finals = list(self.files) if fatal else [remux(p, self.container, self.audio_only)
                                                 for p in self.files]
        many = len(finals) > 1
        lengths = []
        for i, f in enumerate(finals, 1):
            streams, seconds = probe(f)
            lengths.append(seconds)
            has_video = any(k == "Video" for k, _ in streams) or f.suffix.lower() == ".mp4"
            kind = "part" if many else ("video" if has_video else "audio")
            jobs.add_file(self.jid, str(f), kind=kind, label=f"Part {i}" if many else "")
        if fatal:
            raise errors.AppError(fatal, tail, folder=str(self.outdir))

        # The files' own lengths. ffmpeg's running clock can start a few
        # seconds in when it joins a stream behind the live edge, so it is
        # only the fallback.
        duration = sum(lengths) if all(lengths) else self.done_seconds
        size = sum(_size_of(f) for f in finals)
        return {
            "meta": self.target["meta"],
            "detail": {"duration": round(duration), "size": size,
                       "container": finals[0].suffix[1:].lower(), "parts": len(finals),
                       "end_reason": end, "reconnects": self.reconnects,
                       "duration_text": _hms(duration)},
            "output_dir": str(self.outdir),
        }


def _nothing_recorded(code: int, tail: str) -> Exception:
    """The error for a recording that never wrote a byte. The code is chosen
    here rather than by matching the text: ffmpeg's log of a live stream is
    full of URLs and numbers that would read as "404, bad link"."""
    detail = tail.strip() or "The stream stopped before anything was recorded."
    proxy = _proxy()
    if proxy and not proxy.lower().startswith("http://"):
        # The stream link was found through the proxy, but the recording
        # itself cannot use a SOCKS proxy, and YouTube ties a stream link to
        # the address that asked for it. Blaming the site would mislead.
        return errors.AppError(
            "proxy", f"{detail}\nLive recording can only go through an http:// proxy, "
                     f"not {proxy.split('://', 1)[0]}.")
    decode = getattr(errors, "ffmpeg_exit_reason", None)
    decoded = decode(code) if (decode and code) else None
    if decoded:
        return errors.AppError(decoded[0], f"{detail}\n(ffmpeg: {decoded[1]})")
    if re.search(r"\b403\b|forbidden", tail, re.I):
        return errors.AppError("rate_limited", detail)
    return errors.AppError("network" if _TRANSIENT.search(tail) else "unknown", detail)


def _fatal(tail: str) -> str | None:
    """Failures that another session cannot fix."""
    low = tail.lower()
    if "no space left" in low or "not enough space" in low or "disk full" in low:
        return "disk_full"
    if "permission denied" in low or "access is denied" in low:
        return "folder_denied"
    return None


def _size_of(path: Path) -> int:
    try:
        return os.stat(path).st_size      # os.stat sees a growing file's real size
    except OSError:
        return 0


def _plain(msg: str) -> str:
    """yt-dlp's message without 'ERROR: [site] id:' in front or its bug-report tail."""
    msg = re.sub(r"^ERROR:\s*(?:\[[^\]]+\]\s*(?:[^\s:]+:\s+)?)?", "", (msg or "").strip())
    msg = re.split(r";\s*please report this issue", msg, flags=re.I)[0]
    return msg.strip() or "Not live yet."


def _rec_stage(secs: float, size: int, limit: float | None) -> str:
    if limit:
        return f"Recording · {_hms(secs)} of {_hms(limit)} · {_bytes(size)}"
    return f"Recording · {_hms(secs)} · {_bytes(size)}"


def _saving_stage(secs: float) -> str:
    return f"Saving the recording… {_hms(secs)} recorded. Keep the app open."


def _bytes(n: int) -> str:
    n = max(int(n or 0), 0)
    if n < 1024 * 1024:
        return f"{max(n // 1024, 0)} KB"
    if n < 1024 ** 3:
        mb = n / 1024 ** 2
        return f"{mb:.1f} MB" if mb < 10 else f"{mb:.0f} MB"
    return f"{n / 1024 ** 3:.1f} GB"


def _hms(seconds: float) -> str:
    # Rounded like detail["duration"], so a 39.96 s recording reads 0:40 in both.
    s = int(round(max(seconds or 0, 0)))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"
