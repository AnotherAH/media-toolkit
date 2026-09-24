"""Plain-language failures.

Every failure the user sees is one of a fixed set of codes, each with a title,
one sentence of explanation and the buttons that fix it. The raw yt-dlp,
ffmpeg or Python text is kept, cleaned, as ``detail`` for the "Technical
details" disclosure and for bug reports, never as the headline.

Engines raise ``AppError(code, detail)`` when they know the cause. Everything
else goes through ``classify``, which matches the exception text against a
table ordered from the most specific phrase to the broadest, because the same
words show up in unrelated messages ("Sign in to confirm you're not a bot"
mentions --cookies, and almost every ffmpeg-driven failure mentions ffmpeg).

This module must stay importable before yt-dlp is: the launcher uses
``redact`` for its log file.
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# code -> (title, body, actions). Placeholders: {Site}, {host:port}, {folder},
# {Browser}, {Channel}, {kind}. Action ids are the ones the UI knows how to run.
CATALOG: dict[str, tuple[str, str, list[str]]] = {
    "bad_link": (
        "This link doesn't point to a video",
        "Make sure it opens a video in your browser. Profile, search and home-page links "
        "won't work; open the video itself and copy that link.",
        ["edit_link", "remove"]),
    "unavailable": (
        "This video isn't available any more",
        "It was removed or made private, or the link has a typo.",
        ["remove"]),
    "signin": (
        "You need to be signed in to get this",
        "It's private, members-only or age-restricted. Let Media Toolkit use the sign-in "
        "from your browser, then try again.",
        ["signin", "retry"]),
    "age": (
        "This video is age-restricted",
        "It's private, members-only or age-restricted. Let Media Toolkit use the sign-in "
        "from your browser, then try again.",
        ["signin", "retry"]),
    "geo": (
        "This video isn't available in your country",
        "The uploader limits where it can be watched. A VPN or proxy set to an allowed "
        "country usually works.",
        ["retry", "proxy"]),
    "rate_limited": (
        "{Site} is limiting downloads right now",
        "This usually clears in 15 to 30 minutes. Signing in to {Site} also helps.",
        ["retry", "signin"]),
    "proxy": (
        "Couldn't connect through your proxy ({host:port})",
        "Make sure your proxy or VPN app is running, or clear the proxy in Settings.",
        ["proxy", "retry"]),
    "network": (
        "Couldn't connect to the internet",
        "Check your connection, then try again.",
        ["retry"]),
    "ffmpeg_missing": (
        "One more component is needed",
        "Joining video and audio needs ffmpeg, a free one-time download. The download "
        "restarts on its own once it's installed.",
        ["install_ffmpeg"]),
    "convert_failed": (
        "Converting the file didn't work",
        "Try a different format or quality. If you cut out part of the video, check the times.",
        ["retry"]),
    "format_unavailable": (
        "That quality isn't available for this video",
        "Pick another quality or format on the Download tab, or Best available, and try again.",
        ["retry", "remove"]),
    "site_changed": (
        "{Site} changed something on its end",
        "Media Toolkit needs an update to handle this site again. Try again later.",
        ["retry", "copy_details", "update_retry"]),
    "disk_full": (
        "Your drive is full",
        "Free up some space, or choose a different download folder.",
        ["choose_folder", "retry"]),
    "folder_denied": (
        "Can't save to your download folder",
        "Windows blocked access to {folder}. Choose a different folder.",
        ["choose_folder"]),
    "cookies_locked": (
        "Couldn't read your browser's sign-in",
        "Close {Browser} completely and try again, or sign in with a new window instead.",
        ["retry", "signin"]),
    "live_not_live": (
        "{Channel} isn't live right now",
        "Turn on “If it hasn't started yet, wait for it” to record it when it starts.",
        ["retry_wait"]),
    "model_damaged": (
        "The speech model didn't download completely",
        "It downloads again automatically. Try again.",
        ["retry"]),
    "gpu_failed": (
        "Transcribing on the graphics card didn't work",
        "Try again. It will use the processor instead.",
        ["retry"]),
    "out_of_memory": (
        "Not enough graphics memory for this model",
        "Choose a smaller speech model in the Transcript options.",
        ["smaller_model"]),
    "no_speech": (
        "No speech was found",
        "If there is speech, try again with “Skip long silences” turned off.",
        ["retry_novad"]),
    "unreadable_file": (
        "This file couldn't be read",
        "It may be damaged, or it isn't an audio or video file.",
        ["choose_file"]),
    "playlist_not_supported": (
        "This is a playlist",
        "Transcripts work one video at a time. Open a video from it and paste that link.",
        ["edit_link", "remove"]),
    "unknown": (
        "This {kind} didn't work",
        "Try again. If it keeps failing, copy the details and include them when you report "
        "the problem.",
        ["retry", "copy_details"]),
}

# Link previews and the live check have no job to retry; their headline is
# about the link itself.
PROBE_TITLE = "Couldn't read this link"
PROBE_BODY = "Check that the link opens in your browser, then try again."
# yt-dlp's live_status values for a stream that is over (or never was one).
ENDED_STATUSES = ("not_live", "was_live", "post_live")

_KIND_NOUN = {"download": "download", "transcript": "transcript", "live": "recording"}


class AppError(Exception):
    """A failure whose cause is known. ``params`` fill the catalog placeholders
    (Site, Channel, folder, Browser, host) and may carry extra facts for the UI,
    such as title/thumbnail/uploader of a link that turned out not to be live."""

    def __init__(self, code: str, detail: str = "", **params):
        self.code = code if code in CATALOG else "unknown"
        self.detail = detail or ""
        self.params = params
        super().__init__(detail or self.code)


# ------------------------------------------------------------------- sites

_SITES = [
    (("youtube.com", "youtu.be", "youtube-nocookie.com"), "YouTube"),
    (("instagram.com", "cdninstagram.com"), "Instagram"),
    (("tiktok.com",), "TikTok"),
    (("x.com", "twitter.com"), "X"),
    (("facebook.com", "fb.watch", "fb.com"), "Facebook"),
    (("twitch.tv",), "Twitch"),
    (("vimeo.com",), "Vimeo"),
    (("reddit.com", "redd.it"), "Reddit"),
    (("kick.com",), "Kick"),
    (("soundcloud.com",), "SoundCloud"),
    (("dailymotion.com", "dai.ly"), "Dailymotion"),
]


def site_name(url: str) -> str:
    """'YouTube', 'Instagram', ... or the host without 'www.'; '' when the
    text has no host at all."""
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if not host:
        return ""
    for domains, name in _SITES:
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host


# ------------------------------------------------------------ the needles

# (pattern, code). Plain strings are lowercase substrings; compiled patterns
# are searched in the lowercased text. Order is the whole point: first match
# wins, so every phrase that contains a broader rule's words sits above it.
_W = r"(?<![\w.-])"          # a number standing on its own, not inside an id
_E = r"(?![\w-])"
_RULES: list[tuple[str | re.Pattern, str]] = [
    ("could not copy chrome cookie database", "cookies_locked"),
    ("failed to decrypt", "cookies_locked"),
    ("unable to connect to proxy", "proxy"),
    ("proxyerror", "proxy"),
    ("proxy error", "proxy"),
    ("tunnel connection failed", "proxy"),
    ("ffprobe and ffmpeg not found", "ffmpeg_missing"),
    ("ffmpeg not found", "ffmpeg_missing"),
    ("ffprobe not found", "ffmpeg_missing"),
    ("ffmpeg is not installed", "ffmpeg_missing"),
    # Operating-system failures quote file paths, which can contain any word
    # below ("private", "404"), so they are matched first.
    ("no space left on device", "disk_full"),
    ("not enough space on the disk", "disk_full"),
    (re.compile(r"errno 28(?!\d)"), "disk_full"),
    (re.compile(r"errno 13(?!\d)"), "folder_denied"),
    ("access is denied", "folder_denied"),
    ("permission denied", "folder_denied"),
    ("confirm your age", "age"),
    ("age-restricted", "age"),
    ("age restricted", "age"),
    ("inappropriate for some users", "age"),
    ("not a bot", "rate_limited"),              # mentions --cookies too
    # ffmpeg's own words for HTTP failures while it reads a stream
    ("server returned 401", "signin"),
    ("server returned 403", "rate_limited"),
    ("server returned 404", "unavailable"),
    ("server returned 5", "network"),
    # yt-dlp prefixes every post-processor failure with "Postprocessing:".
    # Its text quotes file names and chapter notes ("chapters may have
    # already been removed"), which must not read as a removed video or a
    # private one. SponsorBlock's API is reached from a post-processor too.
    ("unable to communicate with", "network"),
    ("requested format is not available", "format_unavailable"),
    ("postprocessing:", "convert_failed"),
    ("not currently live", "live_not_live"),
    ("live event will begin", "live_not_live"),
    ("will begin in", "live_not_live"),
    ("premieres in", "live_not_live"),
    ("is_upcoming", "live_not_live"),
    ("not available in your country", "geo"),
    ("available in your country", "geo"),
    ("from your location", "geo"),
    ("geo-restrict", "geo"),
    ("geo restrict", "geo"),
    ("georestrict", "geo"),
    ("members-only", "signin"),
    ("members only", "signin"),
    ("join this channel", "signin"),
    ("login required", "signin"),
    ("empty media response", "signin"),
    ("--cookies", "signin"),
    ("registered users", "signin"),
    ("requires authentication", "signin"),
    ("http error 401", "signin"),
    ("private", "signin"),
    ("sign in", "signin"),
    ("too many requests", "rate_limited"),
    (re.compile(_W + r"429" + _E), "rate_limited"),
    ("rate-limit", "rate_limited"),
    ("rate limit", "rate_limited"),
    ("http error 403", "rate_limited"),
    (re.compile(_W + r"404" + _E), "bad_link"),
    ("unsupported url", "bad_link"),
    ("not a valid url", "bad_link"),
    ("video unavailable", "unavailable"),
    ("is unavailable", "unavailable"),
    ("no longer available", "unavailable"),
    ("been removed", "unavailable"),
    ("removed by", "unavailable"),
    ("been terminated", "unavailable"),         # "the account has been terminated"
    ("getaddrinfo", "network"),
    ("name or service not known", "network"),
    ("temporary failure in name resolution", "network"),
    ("failed to resolve", "network"),
    ("timed out", "network"),
    ("unreachable", "network"),
    ("connection reset", "network"),
    ("connection aborted", "network"),
    ("connection refused", "network"),
    ("remote end closed connection", "network"),
    ("unable to open file", "model_damaged"),
    ("did not download correctly", "model_damaged"),
    ("out of memory", "out_of_memory"),
    ("cudnn", "gpu_failed"),
    ("cublas", "gpu_failed"),
    ("cuda failed", "gpu_failed"),
    ("cuda driver", "gpu_failed"),
    ("cuda error", "gpu_failed"),
    ("no speech", "no_speech"),
    ("invalid data found when processing input", "unreadable_file"),
    ("moov atom not found", "unreadable_file"),
    ("does not contain any stream", "unreadable_file"),
    ("ffmpeg exited with code", "convert_failed"),
    ("conversion failed", "convert_failed"),
    ("unable to extract", "site_changed"),
    ("please report this issue", "site_changed"),
]

# Windows error numbers that yt-dlp and Python quote as "[WinError N]".
_WINERR = {
    10060: "network", 10065: "network", 10051: "network", 10054: "network",
    10061: "network", 11001: "network", 11002: "network", 11004: "network",
    112: "disk_full", 39: "disk_full",
    5: "folder_denied", 3: "folder_denied", 21: "folder_denied", 1920: "folder_denied",
}


def ffmpeg_exit_reason(code: int) -> tuple[str, str] | None:
    """Decode an ffmpeg exit status into (catalog code, short explanation).

    ffmpeg exits with a negative AVERROR, which Windows reports as an unsigned
    32-bit number: 3436169992 is AVERROR_HTTP_FORBIDDEN, the site refusing the
    stream, not a broken ffmpeg.
    """
    value = code - (1 << 32) if code >= (1 << 31) else code
    if value >= 0:
        return None
    tag = -value
    if tag < 4096:                                   # AVERROR(errno)
        return {28: ("disk_full", "the drive is full"),
                13: ("folder_denied", "access was denied"),
                5: ("network", "a read or write failed"),
                110: ("network", "the connection timed out"),
                138: ("network", "the connection dropped")}.get(tag)
    b = [(tag >> s) & 0xFF for s in (0, 8, 16, 24)]
    if b[0] != 0xF8:
        return None
    status = "".join(chr(x) for x in b[1:])
    if status == "403":
        return "rate_limited", "the site refused the stream (HTTP 403)"
    if status == "401":
        return "signin", "the site asked for a sign-in (HTTP 401)"
    if status == "404":
        return "unavailable", "the stream was not found (HTTP 404)"
    if status.startswith("5"):
        return "network", "the site had a server error (HTTP 5xx)"
    if status.startswith("4"):
        return "rate_limited", "the site refused the request (HTTP 4xx)"
    return None


def _match(text: str) -> str:
    low = text.lower()
    m = re.search(r"ffmpeg exited with code (\d+)", low)
    if m:
        decoded = ffmpeg_exit_reason(int(m.group(1)))
        if decoded:
            return decoded[0]
    for pattern, code in _RULES:
        if isinstance(pattern, str):
            if pattern in low:
                return code
        elif pattern.search(low):
            return code
    for num in re.findall(r"winerror (\d+)", low):
        code = _WINERR.get(int(num))
        if code:
            return code
    return "unknown"


def _code_for(exc: BaseException) -> str:
    """Exception types that say more than their text does."""
    import errno as _errno
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    if isinstance(exc, PermissionError):
        return "folder_denied"
    if isinstance(exc, OSError) and exc.errno == _errno.ENOSPC:
        return "disk_full"
    return ""


# ----------------------------------------------------------------- details

_PREFIX = re.compile(r"^\s*ERROR:\s*")
_EXTRACTOR = re.compile(r"^\[[^\]\n]+\]\s*")
_VIDEO_ID = re.compile(r"^[^\s:]+:\s+")
_REPORT_TAIL = re.compile(r";?\s*please report this issue on[^\n]*", re.I)


def clean_detail(text: str) -> str:
    """The full message minus yt-dlp's boilerplate: the leading
    'ERROR: [extractor] id: ' and the 'please report this issue' tail."""
    text = (text or "").strip()
    text = _PREFIX.sub("", text, count=1)
    if _EXTRACTOR.match(text):
        text = _EXTRACTOR.sub("", text, count=1)
        text = _VIDEO_ID.sub("", text, count=1)
    text = _REPORT_TAIL.sub("", text)
    return redact(text).strip()


# Query keys that identify a video and are safe to keep in logs and details.
_SAFE_KEYS = {"v", "list", "index", "t", "start", "id", "p", "page", "hl", "lang", "tab"}
# Hosts whose URLs are signed, per-user media links (the path carries the
# signature and the user's IP): keep only the host.
_CDN = ("googlevideo.com", "fbcdn.net", "cdninstagram.com", "tiktokcdn.com",
        "tiktokcdn-us.com", "ttvnw.net", "akamaized.net", "cloudfront.net")
_URL = re.compile(r"https?://[^\s'\"<>]+", re.I)
_SECRET_HEADER = re.compile(
    r"(?i)\b((?:set-)?cookie|authorization|proxy-authorization|x-mt-token)(\s*[:=]\s*)[^\r\n]*")
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
# ffmpeg's own cookie option, in a command line or in the repr of an argument
# list ("-cookies', 'SID=...").
_FFMPEG_COOKIES = re.compile(r"(?i)(-cookies['\"]?\s*,?\s*)(\"[^\"]*\"|'[^']*'|\S+)")


def _redact_url(m: re.Match) -> str:
    raw = m.group(0)
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return raw
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if any(host == c or host.endswith("." + c) for c in _CDN):
        return urlunsplit((parts.scheme, netloc, "/[hidden]", "", ""))
    query = parts.query
    if query:
        kept = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            kept.append(f"{key}={value}" if key.lower() in _SAFE_KEYS else f"{key}=[hidden]")
        query = "&".join(kept)
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def redact(text: str) -> str:
    """Remove secrets from text bound for a log file or a bug report: cookie
    and authorization values, proxy passwords, signed query parameters and
    per-user media CDN links. Video ids and playlist ids survive."""
    if not text:
        return text
    text = _SECRET_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", text)
    text = _FFMPEG_COOKIES.sub(r"\1[hidden]", text)
    text = _USERINFO.sub(r"\1[hidden]@", text)
    return _URL.sub(_redact_url, text)


# ------------------------------------------------------------ assembling

def _proxy_hostport() -> str:
    try:
        from . import config
        proxy = config.get().get("proxy") or ""
        parts = urlsplit(proxy)
        if parts.hostname:
            return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    except Exception:
        pass
    return ""


def _folder_for(kind: str) -> str:
    try:
        from . import config
        cfg = config.get()
        return cfg["transcript_dir"] if kind == "transcript" else cfg["download_dir"]
    except Exception:
        return ""


def _browser_label() -> str:
    try:
        from . import config
        bid = config.get().get("cookies_browser") or ""
    except Exception:
        bid = ""
    labels = {"firefox": "Firefox", "librewolf": "LibreWolf", "chrome": "Chrome",
              "edge": "Edge", "brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi",
              "chromium": "Chromium", "whale": "Whale"}
    return labels.get(bid, "")


def _fill_site(text: str, site: str) -> str:
    if site:
        return text.replace("{Site}", site)
    # "This site" at the start of a sentence, "this site" inside one.
    text = re.sub(r"(^|[.!?]\s+)\{Site\}", lambda m: m.group(1) + "This site", text)
    return text.replace("{Site}", "this site")


def _clock(when) -> str:
    """'22:00' for an epoch time, in this PC's local time; '' when unusable."""
    try:
        return time.strftime("%H:%M", time.localtime(float(when)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _is_link(url: str) -> bool:
    return bool(re.match(r"^https?://", (url or "").strip(), re.I))


def entry(code: str, url: str = "", kind: str = "", detail: str = "", **params) -> dict:
    """The catalog entry for ``code`` with its placeholders filled in."""
    if code not in CATALOG:
        code = "unknown"
    title, body, actions = CATALOG[code]
    site = params.get("Site") or params.get("site") or site_name(url)
    host = params.get("host") or (_proxy_hostport() if code == "proxy" else "")
    folder = params.get("folder") or (_folder_for(kind) if code == "folder_denied" else "")
    browser = params.get("Browser") or params.get("browser") or \
        (_browser_label() if code == "cookies_locked" else "")
    channel = params.get("Channel") or params.get("channel") or params.get("uploader") or ""

    def fill(text: str) -> str:
        text = _fill_site(text, site)
        text = text.replace(" ({host:port})", f" ({host})" if host else "")
        text = text.replace("{host:port}", host or "your proxy")
        text = text.replace("{folder}", folder or "that folder")
        text = text.replace("{Browser}", browser or "your browser")
        text = text.replace("{Channel}", channel or "This channel")
        text = text.replace("{kind}", _KIND_NOUN.get(kind, "task"))
        return text

    out = {"code": code, "title": fill(title), "body": fill(body),
           "actions": list(actions), "detail": detail}
    if kind == "probe" and code == "unknown":
        out.update(title=PROBE_TITLE, body=PROBE_BODY, actions=["copy_details"])
    elif code == "live_not_live" and params.get("live_status") in ENDED_STATUSES:
        # A finished video pasted on the Live tab: waiting would never end.
        out.update(title="This is a regular video, not a live stream",
                   body="The Download tab saves finished videos like this one.",
                   actions=["download_instead", "remove"])
    elif code == "live_not_live" and _clock(params.get("gave_up_at")):
        # The user already asked to wait; telling them to turn waiting on
        # would be wrong. Say when it gave up instead.
        out.update(body=f"Stopped waiting at {_clock(params['gave_up_at'])}. "
                        "It hadn't started by then.",
                   actions=["retry_wait", "remove"])
    elif code == "live_not_live" and kind != "live":
        # A download or transcript of a stream that has not started: the
        # Live tab is where waiting for it happens.
        out.update(body="The Live tab can wait for it to start and record it.",
                   actions=["record_live", "remove"])
    elif code == "unreadable_file" and kind != "probe" and _is_link(url):
        # Media fetched from a link that ffmpeg could not decode: there is no
        # file of the user's to swap for another one.
        if kind == "transcript":
            out["title"] = "The sound in this video couldn't be read"
        out.update(body="The site sent something Media Toolkit can't open. Try again later.",
                   actions=["retry", "copy_details"])
    extra = {k: v for k, v in params.items()
             if k in ("title", "thumbnail", "uploader", "release_timestamp", "live_status",
                      "gave_up_at")
             and v not in (None, "")}
    if extra:
        out["params"] = extra
    return out


def classify(exc: BaseException, url: str = "", kind: str = "") -> dict:
    """{code, title, body, actions, detail} for any exception."""
    if isinstance(exc, AppError):
        detail = clean_detail(exc.detail) if exc.detail else ""
        return entry(exc.code, url, kind, detail, **exc.params)
    text = str(exc).strip() or exc.__class__.__name__
    code = _match(text)
    if code == "unknown":
        code = _code_for(exc) or "unknown"
    detail = clean_detail(text)
    if code == "unknown" and exc.__class__.__name__ not in text:
        detail = f"{exc.__class__.__name__}: {detail}"
    m = re.search(r"ffmpeg exited with code (\d+)", text.lower())
    if m and (decoded := ffmpeg_exit_reason(int(m.group(1)))):
        detail = f"{detail}\n(ffmpeg: {decoded[1]})"
    return entry(code, url, kind, detail)


def message(err: dict | None) -> str:
    """The legacy one-line job message: title then body."""
    if not err:
        return ""
    return f"{err.get('title', '')} {err.get('body', '')}".strip()
