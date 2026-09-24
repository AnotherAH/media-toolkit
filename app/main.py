"""FastAPI surface: static UI, job API, and an SSE stream for live progress.

Security model. The server listens on loopback, but every web page the user
visits can also send requests to 127.0.0.1, and a DNS-rebinding page can even
read the answers. So every request must name this server in its Host header,
a request that carries an Origin must come from this app's own page, and a
request marked cross-site by the browser (Sec-Fetch-Site) never reaches an
/api route, which keeps an <img> tag from turning the link preview into a
proxy into the user's network. Every request that changes something must
also carry a per-launch token that the server writes into index.html, which
another origin cannot read. /api/goodbye is exempt because navigator.sendBeacon
cannot set headers; the worst it can do is let an idle app quit.

Nothing a request says may choose where files go (output_dir,
output_template, local_path) or which program runs (external_downloader):
folders and patterns come only from the saved settings.
"""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from . import (assets, config, cookies, errors, ffmpegtools, folderpick, hardware, jobs,
               live, media, models, subs, transcribe)

try:
    from . import __version__
except ImportError:                      # pragma: no cover - app/__init__ always has it
    __version__ = "0.0.0"

config.bootstrap()
config.ensure_dirs()
jobs.register("download", media.run_download)
jobs.register("transcript", transcribe.run_transcript)
jobs.register("live", live.run_live, pool="live")
jobs.load_history()

# Per-launch secret. index.html carries it to the app's own page; the
# launcher keeps it in instance.json so a second launch can prove it is
# talking to this server and not some other program on the port.
TOKEN = secrets.token_urlsafe(32)
# Set by the launcher when it binds something other than loopback: then the
# token is required on every /api request and on the page itself.
REMOTE = False
# Development only: tolerate a missing token (still refuses a wrong one), so
# curl and the 1.1 UI keep working until the page sends it.
DEV_NO_TOKEN = os.environ.get("MEDIA_TOOLKIT_DEV_NO_TOKEN") == "1"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
TOKEN_EXEMPT = ("/api/goodbye",)


def configure(remote: bool = False) -> None:
    global REMOTE
    REMOTE = bool(remote)


def cleanup_temp(max_age: float = 24 * 3600, root: Path | None = None) -> int:
    """Delete leftovers in %TEMP%\\media-toolkit older than a day: uploads of
    failed jobs, staging from crashed downloads. Newer files may still belong
    to a job that can be retried, and a folder is only removed when it is both
    empty and old, so a job that just made its folder never loses it."""
    root = root or Path(tempfile.gettempdir()) / "media-toolkit"
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age
    removed = 0
    # Folder ages are read before anything inside them is deleted: removing a
    # file updates its folder's time, and an old per-job folder (a crashed
    # transcript's audio, a download's staging) would otherwise survive
    # until the launch after next.
    ages: dict[str, float] = {}
    for dirpath, _dirs, _files in os.walk(root):
        try:
            ages[dirpath] = os.stat(dirpath).st_mtime
        except OSError:
            pass
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        folder = Path(dirpath)
        if folder in (root, jobs.UPLOAD_ROOT):
            continue
        try:
            if ages.get(dirpath, time.time()) < cutoff:
                folder.rmdir()                  # only succeeds when empty
        except OSError:
            pass
    return removed


def _startup_cleanup() -> None:
    try:
        cookies.remove_stale_copies()
    except Exception as exc:                      # never block startup
        print(f"cookie copy clean-up failed: {exc}")
    cleanup_temp()
    # Test the video encoders now, so the first Settings visit or re-encoded
    # download does not wait for the one-frame trial encodes.
    try:
        ffmpegtools.warm_up()
    except Exception as exc:
        print(f"encoder probe failed: {exc}")


@asynccontextmanager
async def _lifespan(_app):
    threading.Thread(target=_startup_cleanup, name="temp-cleanup", daemon=True).start()
    yield


app = FastAPI(title="Media Toolkit", version=__version__, docs_url="/api/docs",
              redoc_url=None, openapi_url="/api/openapi.json", lifespan=_lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ------------------------------------------------------------------ security

def _deny(message: str, status: int = 403) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _token_from(headers: dict, scope: dict, allow_passive: bool) -> str:
    token = headers.get("x-mt-token", "")
    if token or not allow_passive:
        return token
    # EventSource and plain navigations cannot set headers: in remote mode the
    # token may also come from the query string or the page's own cookie.
    query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
    if query.get("token"):
        return query["token"]
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "mt_token":
            return value
    return ""


def _token_ok(token: str) -> bool:
    return bool(token) and hmac.compare_digest(token.encode(), TOKEN.encode())


def check_request(scope: dict, headers: dict) -> str | None:
    """Why this request must be refused, or None. Pure, so tests can call it."""
    path = scope.get("path", "")
    method = scope.get("method", "GET").upper()
    port = (scope.get("server") or ("", None))[1]
    host = headers.get("host", "").lower()
    names = {f"{n}:{port}" for n in LOOPBACK_NAMES}
    if port == 80:                       # browsers leave the default port out
        names.update(LOOPBACK_NAMES)
    if not REMOTE and host not in names:
        return "This server only answers to its own address."
    origin = headers.get("origin")
    if origin is not None and origin.lower() != f"http://{host}":
        return "Requests from other sites are not allowed."
    is_api = path.startswith("/api/")
    if is_api and headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return "Requests from other sites are not allowed."
    safe = method in ("GET", "HEAD")
    if REMOTE and (is_api or path in ("/", "/index.html")):
        if not _token_ok(_token_from(headers, scope, allow_passive=safe or path in TOKEN_EXEMPT)):
            return "Open the address Media Toolkit printed when it started; it includes the access key."
        return None
    if is_api and not safe and path not in TOKEN_EXEMPT:
        token = headers.get("x-mt-token", "")
        if not token and DEV_NO_TOKEN:
            return None
        if not _token_ok(token):
            return "Missing or wrong access key. Reload the page."
    return None


class Guard:
    """Pure ASGI middleware: the checks above, plus no-store on the page and
    its assets (the app window keeps a persistent profile, so a cached UI
    would survive an update and run against a newer backend)."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        problem = check_request(scope, headers)
        if problem:
            await _deny(problem)(scope, receive, send)
            return
        is_api = scope.get("path", "").startswith("/api/")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "no-referrer"
                if not is_api:
                    h["Cache-Control"] = "no-store, must-revalidate"
                    h["X-Frame-Options"] = "DENY"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(Guard)


class FieldError(Exception):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field, self.message = field, message


def _bad(field: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status, detail={"field": field, "message": message})


_META = re.compile(r'<meta\s+name="mt-token"[^>]*>', re.I)


def _with_token(html: str) -> str:
    tag = f'<meta name="mt-token" content="{TOKEN}">'
    if _META.search(html):
        return _META.sub(tag, html, count=1)
    return re.sub(r"(<head[^>]*>)", r"\1\n" + tag, html, count=1, flags=re.I)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    resp = HTMLResponse(_with_token((STATIC / "index.html").read_text("utf-8")))
    if REMOTE:
        resp.set_cookie("mt_token", TOKEN, httponly=True, samesite="strict")
    return resp


@app.get("/api/instance", include_in_schema=False)
def api_instance(request: Request):
    """Lets a second launch confirm this is our server before reattaching."""
    if not _token_ok(request.headers.get("x-mt-token", "")):
        raise HTTPException(403, "Wrong access key")
    return {"ok": True, "pid": os.getpid(), "version": __version__,
            "active": jobs.active_count()}


# ------------------------------------------------------------------ helpers

_HTTP_URL = re.compile(r"^https?://\S+$", re.I)


def _http_url(url: str, kind: str = "probe") -> str:
    url = (url or "").strip()
    if not _HTTP_URL.match(url) or len(url) > 4096:
        raise HTTPException(400, detail=errors.entry("bad_link", url, kind))
    return url


def _model_ok(name: str) -> bool:
    """A speech-model name straight from the catalog. The name becomes a
    folder that can be deleted, so nothing else may get near the disk."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) \
            or ".." in name:
        return False
    known = getattr(models, "known", None)
    if known is not None:
        return bool(known(name))
    return name in {m["id"] for m in getattr(transcribe, "MODELS", [])}


# Request keys that would let a request choose where files are written or
# which local file is read. The server sets local_path itself for uploads.
_SERVER_ONLY = ("output_dir", "output_template", "local_path", "local_name")


def _clean_options(opts, kind: str = "") -> dict:
    if opts is None:
        return {}
    if not isinstance(opts, dict):
        raise _bad("options", "Options must be an object.")
    out = {k: v for k, v in opts.items()
           if isinstance(k, str) and k not in _SERVER_ONLY and not k.startswith("_")}
    model = out.get("model")
    if model not in (None, "") and not _model_ok(str(model)):
        raise _bad("model", "Unknown speech model.")
    return out


def _hw_summary() -> dict:
    hw = hardware.summary()
    gpus = hw.get("gpus") or []
    hw.setdefault("nvidia_name", gpus[0].get("name", "") if gpus else "")
    hw.setdefault("gpu_ready", bool(hw.get("cuda")) and bool(hw.get("nvidia_name")))
    hw.setdefault("gpu_pack_size_mb", 740)
    return hw


def _yt_dlp_version() -> str:
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return "?"


# ------------------------------------------------------------------ metadata

@app.get("/api/hardware")
def api_hardware():
    hw = _hw_summary()
    hw["models"] = transcribe.model_catalog()
    hw["python"] = sys.version.split()[0]
    hw["yt_dlp"] = _yt_dlp_version()
    return hw


@app.post("/api/hardware/recheck")
async def api_hardware_recheck():
    """Settings > Re-check hardware, after a driver update: forget the
    remembered transcription backends and the CUDA probe, and test the video
    encoders again. Returns the fresh hardware summary."""
    def recheck() -> dict:
        hardware.forget()
        encoders = ffmpegtools.refresh()
        hw = _hw_summary()
        hw["encoders"] = encoders
        return hw
    return await asyncio.to_thread(recheck)


@app.get("/api/capabilities")
def api_capabilities():
    """Everything optional that this machine can or cannot do."""
    # What yt-dlp will really use: media also looks in the app's own bin
    # folders, not only on PATH.
    js = media.js_runtime_name()
    targets = media.impersonate_targets()
    encoders = ffmpegtools.encoder_catalog()
    return {
        "encoders": encoders,
        "hardware": [e["id"] for e in encoders if e.get("hw")],
        "qualities": list(ffmpegtools.QUALITY.keys()),
        "impersonate": targets,
        "impersonate_available": bool(targets),
        "browsers": cookies.installed(),
        "js_runtime": js,
        "aria2c": bool(shutil.which("aria2c")),
        "sponsor_categories": getattr(media, "SPONSOR_CATEGORIES", []),
        "sponsor_labels": dict(getattr(media, "SPONSOR_LABELS", {})),
        "audio_codecs": list(getattr(media, "AUDIO_CODECS", ())),
        "quality_presets": list(getattr(media, "VIDEO_PRESETS", {}).keys()),
        "quality_labels": dict(getattr(media, "VIDEO_PRESETS", {})),
    }


def _updater():
    try:
        from . import updater
        return updater
    except ImportError:
        return None


@app.get("/api/about")
def api_about():
    notices = config.ROOT / "THIRD-PARTY-NOTICES.txt"
    up = _updater()
    try:
        activation = up.activation() if up is not None and hasattr(up, "activation") else {}
    except Exception:
        activation = {}
    return {
        "version": __version__,
        "yt_dlp": _yt_dlp_version(),
        # True when the yt-dlp in use is one the user downloaded (Update site
        # support) rather than the copy that shipped with the app.
        "yt_dlp_updated": bool(activation.get("active")),
        "can_update_ytdlp": bool(up is not None and hasattr(up, "update")),
        "python": sys.version.split()[0],
        "frozen": bool(config.FROZEN),
        "portable": bool(getattr(config, "PORTABLE", False)),
        "data_dir": str(config.DATA_ROOT),
        "log_path": str(config.LOG_PATH),
        "notices_path": str(notices) if notices.exists() else "",
    }


@app.post("/api/update-ytdlp")
async def api_update():
    """Site support breaks when sites change; fetch the newest yt-dlp."""
    up = _updater()
    if up is None or not hasattr(up, "update"):
        return {"ok": False, "version": _yt_dlp_version(),
                "message": "Updating isn't available in this build.", "restart_required": False}
    try:
        return await asyncio.to_thread(up.update)
    except Exception as exc:
        return {"ok": False, "version": _yt_dlp_version(),
                "message": errors.clean_detail(str(exc))[:300], "restart_required": False}


@app.get("/api/update-check")
@app.get("/api/about/check", include_in_schema=False)
async def api_update_check():
    """Is a newer Media Toolkit release out? Runs only when the user asks."""
    up = _updater()
    if up is None or not hasattr(up, "check_app_update"):
        raise HTTPException(501, "Update checks aren't available in this build.")
    try:
        return await asyncio.to_thread(up.check_app_update)
    except Exception as exc:
        raise HTTPException(502, f"Couldn't check for updates: {errors.clean_detail(str(exc))[:200]}")


# ------------------------------------------------------------------ settings

_BROWSER_IDS = {b["id"] for b in cookies.BROWSERS}
_PROXY_RE = re.compile(r"^(https?|socks4a?|socks5h?)://\S+$", re.I)
_RATE_RE = re.compile(r"^\d+(\.\d+)?\s*[KMG]?(i?B)?$", re.I)
_CHOICES = {
    "whisper_device": ("auto", "cuda", "cpu"),
    "whisper_compute": ("auto", "float16", "int8_float16", "int8", "float32", "bfloat16",
                        "int8_bfloat16", "int8_float32"),
    "sponsorblock_mode": ("remove", "mark"),
    "convert_thumbnails": ("", "jpg", "png"),
    "external_downloader": ("", "aria2c"),
    "filename_preset": tuple(config.FILENAME_PRESETS) + ("custom",),
}


_WHOLE_NUMBERS = ("concurrent_fragments", "whisper_beam", "dl_max_comments")


def check_folder(path: str) -> str | None:
    """Create the folder and prove a file can be written there. Returns the
    problem in plain words, or None when it is usable."""
    raw = os.path.expanduser((path or "").strip())
    if not raw:
        return "Choose a folder."
    if not os.path.isabs(raw):
        return ("Use a full folder path, for example "
                + ("C:\\Users\\you\\Videos." if os.name == "nt" else "/Users/you/Movies."
                   if sys.platform == "darwin" else "/home/you/Videos."))
    folder = Path(raw)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Can't use this folder: {config.folder_reason(exc)}"
    if not folder.is_dir():
        return "Can't use this folder: a file with that name is in the way"
    probe = folder / f".media-toolkit-write-test-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"ok")
    except OSError as exc:
        return f"Can't use this folder: {config.folder_reason(exc)}"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return None


def _coerce(key: str, value):
    default = config.DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1", "true", "false", "True", "False"):
            return str(value).lower() in ("1", "true")
        raise FieldError(key, "Expected on or off.")
    if isinstance(default, int):
        try:
            number = float(str(value).strip() or 0) if not isinstance(value, bool) else int(value)
        except ValueError:
            raise FieldError(key, "Enter a number.") from None
        if number != number or abs(number) > 1e9:
            raise FieldError(key, "Enter a number.")
        if number == int(number):
            return int(number)
        if key in _WHOLE_NUMBERS:
            raise FieldError(key, "Enter a whole number.")
        return number              # e.g. "give up after 1.5 hours"
    if isinstance(default, float):
        try:
            number = float(str(value).strip() or 0)
        except ValueError:
            raise FieldError(key, "Enter a number.") from None
        if number != number or number < 0 or number > 1e6:
            raise FieldError(key, "Enter 0 or more.")
        return number
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise FieldError(key, "Expected text.")
    text = str(value).strip()
    if any(c in text for c in "\r\n\x00"):
        raise FieldError(key, "Line breaks aren't allowed here.")
    if len(text) > 2000:
        raise FieldError(key, "That's too long.")
    return text


def validate_settings(patch: dict, recheck_folders: bool = False) -> dict:
    """Checked, typed subset of ``patch`` ready for config.save. Raises
    FieldError naming the first field that cannot be saved. First-run setup
    passes recheck_folders: there every folder must be proven usable."""
    if not isinstance(patch, dict):
        raise FieldError("", "Settings must be an object.")
    if isinstance(patch.get("sponsorblock_categories"), list):      # a list of checkboxes
        patch = dict(patch, sponsorblock_categories=",".join(
            str(c) for c in patch["sponsorblock_categories"]))
    out = {k: _coerce(k, v) for k, v in patch.items() if k in config.DEFAULTS}

    current = config.get()
    for key in ("download_dir", "transcript_dir"):
        if key in out:
            # A page that saves every field at once resends the folders too.
            # Re-testing an unchanged one would block every other setting
            # while its drive is unplugged; Settings already shows that.
            if out[key] == current.get(key) and not recheck_folders:
                continue
            problem = check_folder(out[key])
            if problem:
                raise FieldError(key, problem)
            out[key] = os.path.expanduser(out[key])
    if out.get("proxy") and not _PROXY_RE.match(out["proxy"]):
        raise FieldError("proxy", "Start with http://, https://, socks4:// or socks5://")
    for key, allowed in _CHOICES.items():
        if key in out and out[key] not in allowed:
            if key == "external_downloader":
                raise FieldError(key, "Only aria2c can be used as an external downloader.")
            raise FieldError(key, "That choice isn't available.")
    if "concurrent_fragments" in out and not 1 <= out["concurrent_fragments"] <= 16:
        raise FieldError("concurrent_fragments", "Choose between 1 and 16.")
    if "whisper_beam" in out and not 1 <= out["whisper_beam"] <= 10:
        raise FieldError("whisper_beam", "Choose between 1 and 10.")
    if "dl_max_comments" in out and not 0 <= out["dl_max_comments"] <= 100000:
        raise FieldError("dl_max_comments", "Choose between 0 and 100,000.")
    if "lv_wait_hours" in out and not 0 < out["lv_wait_hours"] <= 168:
        raise FieldError("lv_wait_hours", "Enter a number of hours, up to 168.")
    for key in ("rate_limit", "throttled_rate"):
        if out.get(key) and not _RATE_RE.match(out[key]):
            raise FieldError(key, "Enter a speed such as 5M or 500K.")
    if out.get("geo_bypass_country"):
        if not re.fullmatch(r"[A-Za-z]{2}", out["geo_bypass_country"]):
            raise FieldError("geo_bypass_country", "Use a two-letter country code such as US.")
        out["geo_bypass_country"] = out["geo_bypass_country"].upper()
    if out.get("impersonate"):
        out["impersonate"] = out["impersonate"].lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9:._-]{0,40}", out["impersonate"]):
            raise FieldError("impersonate", "That choice isn't available.")
    if "sponsorblock_categories" in out:
        known = getattr(media, "SPONSOR_CATEGORIES", [])
        cats = [c.strip().lower() for c in out["sponsorblock_categories"].split(",") if c.strip()]
        if not cats:
            raise FieldError("sponsorblock_categories", "Choose at least one kind of segment.")
        if any(c not in known for c in cats):
            raise FieldError("sponsorblock_categories", "That choice isn't available.")
        out["sponsorblock_categories"] = ",".join(dict.fromkeys(cats))
    if out.get("cookies_browser") and out["cookies_browser"] not in _BROWSER_IDS:
        raise FieldError("cookies_browser", "That browser isn't supported.")
    if out.get("cookies_file"):
        if not Path(os.path.expanduser(out["cookies_file"])).is_file():
            raise FieldError("cookies_file", "That file doesn't exist.")
    if out.get("whisper_model") and not _model_ok(out["whisper_model"]):
        raise FieldError("whisper_model", "Unknown speech model.")

    # File names: a preset writes its pattern; a hand-written pattern is custom.
    preset = out.get("filename_preset")
    if preset and preset != "custom":
        out["output_template"] = config.FILENAME_PRESETS[preset]
    elif "output_template" in out:
        if not config.output_template_ok(out["output_template"]):
            raise FieldError("output_template",
                             "Use a file name pattern without a drive letter or '..'.")
        if preset is None:
            match = [k for k, v in config.FILENAME_PRESETS.items() if v == out["output_template"]]
            out["filename_preset"] = match[0] if match else "custom"

    # One sign-in source at a time: a browser and a cookies file together
    # silently fought over which one yt-dlp used.
    if out.get("cookies_browser") and out.get("cookies_file"):
        raise FieldError("cookies_file", "Choose either a browser or a cookies file, not both.")
    if out.get("cookies_browser"):
        out["cookies_file"] = ""
    elif out.get("cookies_file"):
        out.update(cookies_browser="", cookies_profile="", cookies_container="")
    return out


def _cookies_saved(cfg: dict) -> float:
    """When the sign-in file in use was last written (Settings: 'Using a
    sign-in saved on 23 Sep'), or 0 when there is none."""
    path = str(cfg.get("cookies_file") or "").strip()
    if not path:
        return 0.0
    try:
        return Path(os.path.expanduser(path)).stat().st_mtime
    except (OSError, ValueError):
        return 0.0


@app.get("/api/settings")
def api_get_settings():
    cfg = config.get()
    return {"config": cfg, "defaults": config.DEFAULTS,
            "filename_presets": config.FILENAME_PRESETS,
            "quality_presets": list(getattr(media, "VIDEO_PRESETS", {}).keys()),
            "audio_codecs": list(getattr(media, "AUDIO_CODECS", ())),
            "sponsor_categories": getattr(media, "SPONSOR_CATEGORIES", []),
            "sponsor_labels": dict(getattr(media, "SPONSOR_LABELS", {})),
            "folder_problems": dict(config.FOLDER_PROBLEMS),
            "notice": config.NOTICE,
            "cookies_saved": _cookies_saved(cfg)}


@app.post("/api/settings")
def api_set_settings(patch: dict = Body(...)):
    try:
        clean = validate_settings(patch)
    except FieldError as exc:
        raise _bad(exc.field, exc.message)
    if {"whisper_model", "whisper_device", "whisper_compute"} & set(clean):
        hardware.forget()          # re-prove the backend after a device change
    cfg = config.save(clean)
    if "proxy" in clean:
        config.apply_proxy_env(cfg["proxy"])
    if {"download_dir", "transcript_dir"} & set(clean):
        config.ensure_dirs()
    return {"config": cfg}


# ------------------------------------------------------------- first-run setup

@app.get("/api/setup")
def api_setup_state():
    cfg = config.get()
    hw = _hw_summary()
    home = Path.home()
    videos = folderpick.known_folder("videos") or str(home / "Videos")
    documents = folderpick.known_folder("documents") or str(home / "Documents")
    portable = bool(getattr(config, "PORTABLE", False))
    if portable:
        # A portable copy keeps everything on its own drive: recommending
        # Videos and Documents would leave files behind on this PC.
        recommended = (config.DATA_ROOT / "downloads", config.DATA_ROOT / "transcripts")
    else:
        recommended = (Path(videos) / "Media Toolkit", Path(documents) / "Transcripts")
    return {
        "needed": not cfg["setup_complete"],
        "config": cfg,
        "hardware": hw,
        "folder_problems": dict(config.FOLDER_PROBLEMS),
        "notice": config.NOTICE,
        "suggestions": {
            "home": str(home),
            "portable": portable,
            "download_dir": str(recommended[0]),
            "transcript_dir": str(recommended[1]),
            "portable_download_dir": str(config.DATA_ROOT / "downloads"),
            "portable_transcript_dir": str(config.DATA_ROOT / "transcripts"),
            "whisper_model": hw.get("recommended_model", "small"),
            "nvidia_name": hw.get("nvidia_name", ""),
            "gpu_ready": bool(hw.get("gpu_ready")),
            "gpu_pack_size_mb": hw.get("gpu_pack_size_mb", 740),
        },
    }


@app.post("/api/setup")
def api_setup_save(patch: dict = Body(...)):
    """Folders are proven usable before anything is saved: a path that
    cannot be created must never end up in config.json."""
    patch = dict(patch or {})
    for key in ("download_dir", "transcript_dir"):
        if not str(patch.get(key) or "").strip():
            raise _bad(key, "Choose a folder for both downloads and transcripts.")
    try:
        clean = validate_settings(patch, recheck_folders=True)
    except FieldError as exc:
        raise _bad(exc.field, exc.message)
    clean["setup_complete"] = True
    cfg = config.save(clean)
    config.ensure_dirs()
    return {"config": cfg}


@app.post("/api/pick-folder")
async def api_pick_folder(body: dict = Body(default={})):
    """Native folder chooser, in its own process so a dialog can never wedge
    the server."""
    start = str((body or {}).get("path") or "") or str(Path.home())
    try:
        picked = await asyncio.to_thread(folderpick.choose, start)
    except Exception as exc:
        raise HTTPException(500, f"The folder picker isn't available: {exc}")
    return {"path": picked}


@app.post("/api/pick-file")
async def api_pick_file(body: dict = Body(default={})):
    """Native file chooser (for a cookies.txt)."""
    start = str((body or {}).get("path") or "") or str(Path.home())
    try:
        picked = await asyncio.to_thread(folderpick.choose_file, start,
                                         str((body or {}).get("kind") or ""))
    except Exception as exc:
        raise HTTPException(500, f"The file picker isn't available: {exc}")
    return {"path": picked}


# ------------------------------------------------------------ link previews

@app.get("/api/formats")
async def api_formats(url: str = ""):
    url = _http_url(url)
    try:
        return await asyncio.to_thread(media.list_formats, url)
    except Exception as exc:
        raise HTTPException(400, detail=errors.classify(exc, url, "probe"))


@app.get("/api/probe")
async def api_probe(url: str = ""):
    url = _http_url(url)
    try:
        return await asyncio.to_thread(media.probe, url)
    except Exception as exc:
        raise HTTPException(400, detail=errors.classify(exc, url, "probe"))


_NOT_YET = ("not currently live", "will begin", "premieres", "is_upcoming", "upcoming", "offline")
_ENDED = ("not_live", "was_live", "post_live")
_LIVE_QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360", "worst", "audio")


def _offline_answer(url: str, exc: BaseException) -> dict | None:
    """The live check's answer for an offline channel or a stream that has
    not started, from whichever shape the recorder's exception has."""
    info = dict(getattr(exc, "meta", None) or getattr(exc, "params", None) or {})
    status = info.get("live_status") or ""
    text = str(exc).lower()
    base = {"is_live": False, "title": info.get("title", ""),
            "uploader": info.get("uploader", ""), "thumbnail": info.get("thumbnail", ""),
            "has_video": False, "has_audio": False, "height": 0,
            "site": errors.site_name(url)}
    if status in _ENDED:
        return {**base, "offline": False, "upcoming": False, "regular": True,
                "live_status": status, "release_timestamp": None, "reason": ""}
    not_yet = exc.__class__.__name__ == "NotLiveYet" or \
        errors.classify(exc, url, "probe")["code"] == "live_not_live" or \
        any(k in text for k in _NOT_YET)
    if not not_yet:
        return None
    upcoming = bool(getattr(exc, "upcoming", False)) or status == "is_upcoming"
    return {**base, "offline": True, "upcoming": upcoming, "regular": False,
            "live_status": "is_upcoming" if upcoming else (status or "offline"),
            "release_timestamp": info.get("release_timestamp"),
            "reason": errors.clean_detail(str(exc))}


@app.get("/api/live-check")
async def api_live_check(url: str = "", quality: str = "best", audio_only: bool = False):
    """Is this link live right now, and what can be recorded from it? An
    offline channel or a scheduled stream is an answer, not an error. The
    quality is the one picked on the Live tab, so the preview never promises
    4K while 480p is selected."""
    url = _http_url(url)
    allowed = getattr(live, "QUALITY_SELECTORS", None) or _LIVE_QUALITIES
    quality = quality if quality in allowed else "best"
    check = getattr(live, "check", None)
    try:
        if check is not None:
            return await asyncio.to_thread(check, url, quality, bool(audio_only))
        info = await asyncio.to_thread(live.resolve, url, quality, bool(audio_only))
    except Exception as exc:
        answer = _offline_answer(url, exc)
        if answer is not None:
            return answer
        raise HTTPException(400, detail=errors.classify(exc, url, "probe"))
    # Older recorder without check(): describe what resolve() found.
    meta = info.get("meta") or {}
    is_live = bool(info.get("is_live"))
    status = info.get("live_status") or ("is_live" if is_live else "not_live")
    video, audio = info.get("video") or {}, info.get("audio") or {}
    return {
        "is_live": is_live,
        "offline": False,
        "upcoming": False,
        "regular": not is_live and status in _ENDED,
        "live_status": status,
        "title": info.get("title") or meta.get("title", ""),
        "uploader": info.get("uploader") or meta.get("uploader") or meta.get("channel") or "",
        "thumbnail": info.get("thumbnail") or meta.get("thumbnail", ""),
        "release_timestamp": meta.get("release_timestamp") or info.get("release_timestamp"),
        "has_video": bool(video),
        "has_audio": bool(audio or info.get("single")),
        "height": 0 if audio_only else (video.get("height", 0) or 0),
        "site": errors.site_name(url),
        "reason": "",
    }


_PRETTY_SITES = {"youtube": "YouTube", "tiktok": "TikTok", "twitter": "X (Twitter)",
                 "instagram": "Instagram", "facebook": "Facebook", "vimeo": "Vimeo",
                 "reddit": "Reddit", "twitch": "Twitch", "soundcloud": "SoundCloud",
                 "dailymotion": "Dailymotion", "kick": "Kick", "bilibili": "Bilibili",
                 "bandcamp": "Bandcamp", "niconico": "Niconico", "rumble": "Rumble"}


@functools.lru_cache(maxsize=1)
def site_groups() -> tuple[tuple[str, str], ...]:
    """yt-dlp's extractors folded into sites: 'instagram:story' and
    'InstagramIOS' are both Instagram. Returns (display name, key) pairs."""
    from yt_dlp.extractor import list_extractor_classes
    variants: dict[str, list[str]] = {}
    for cls in list_extractor_classes():
        name = getattr(cls, "IE_NAME", "") or ""
        if not name or name.lower().startswith("generic"):
            continue
        base = name.split(":")[0]
        if base:
            variants.setdefault(base.lower(), []).append(base)
    # Fold 'InstagramIOS' into 'instagram': a known site name followed by a
    # capitalised word. Short names (ABC, CBS) stay apart from ABCNews.
    parent: dict[str, str] = {}
    for key in sorted(variants, key=len):
        for cut in range(5, len(key)):
            root = key[:cut]
            if root in variants and root not in parent:
                cased = variants[key][0]
                if len(cased) > cut and cased[cut].isupper():
                    parent[key] = root
                    break
    groups: dict[str, list[str]] = {}
    for key, names in variants.items():
        groups.setdefault(parent.get(key, key), []).extend(names)
    out = []
    for key, names in groups.items():
        if key in _PRETTY_SITES:
            display = _PRETTY_SITES[key]
        else:
            same = sorted({n for n in names if n.lower() == key}, key=lambda n: (not n[:1].isupper(), n))
            display = same[0] if same else key
        out.append((display, key))
    out.sort(key=lambda p: p[0].lower())
    return tuple(out)


@app.get("/api/sites")
def api_sites(q: str = "", limit: int = 400):
    groups = site_groups()
    q = q.strip().lower()
    if not q:
        return {"total": len(groups), "matches": [], "truncated": False, "count": 0}
    hits = [d for d, k in groups if q in d.lower() or q in k]
    hits.sort(key=lambda d: (not d.lower().startswith(q), d.lower()))
    limit = max(1, min(limit, 2000))
    return {"total": len(groups), "matches": hits[:limit], "truncated": len(hits) > limit,
            "count": len(hits)}


# ------------------------------------------------------------------- models

@app.get("/api/models")
def api_models():
    """Which Whisper models are downloaded, and are they intact?"""
    catalog = {m["id"]: m for m in getattr(transcribe, "MODELS", [])}
    rows = models.installed()
    for row in rows:
        info = catalog.get(row.get("name"), {})
        row.setdefault("label", info.get("label") or row.get("name"))
        if info.get("size_mb") and not row.get("size_mb"):
            row["size_mb"] = info["size_mb"]
    state = getattr(models, "download_state", None)
    return {"models": rows, "cache": str(models.cache_root()),
            "downloads": state() if state else []}


def _model_name(body) -> str:
    name = str((body or {}).get("name") or "") if isinstance(body, dict) else ""
    if not _model_ok(name):
        raise _bad("name", "Unknown speech model.")
    return name


@app.post("/api/models/repair")
@app.post("/api/models/delete")
def api_models_repair(body: dict = Body(...)):
    """Delete a model so it is fetched fresh next time. Only catalog names:
    the name becomes a folder path that is deleted."""
    name = _model_name(body)
    try:
        models.purge(name)
    except ValueError:
        raise _bad("name", "Unknown speech model.")
    except RuntimeError as exc:                 # it is downloading right now
        raise HTTPException(409, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"Couldn't delete the model: {exc.strerror or exc}")
    return {"ok": True, "name": name}


@app.post("/api/models/download")
def api_models_download(body: dict = Body(...)):
    """'Download again now' for a damaged model: runs in the background; the
    progress shows up in GET /api/models under downloads."""
    name = _model_name(body)
    start = getattr(models, "start_download", None)
    if start is None:
        raise HTTPException(501, "Downloading a model from here isn't available in this build.")
    try:
        return start(name)
    except ValueError:
        raise _bad("name", "Unknown speech model.")


# ------------------------------------------------------------- runtime packs

@app.get("/api/packs")
def api_packs():
    return assets.status()


@app.post("/api/packs/gpu")
async def api_install_gpu():
    return await asyncio.to_thread(assets.install_gpu_pack)


@app.post("/api/packs/ffmpeg")
async def api_install_ffmpeg():
    return await asyncio.to_thread(assets.install_ffmpeg)


@app.post("/api/packs/gpu/remove")
def api_remove_gpu():
    return assets.remove_gpu_pack()


# ------------------------------------------------------------------- cookies

def _cookie_jar() -> Path:
    return config.DATA_ROOT / "cookies.txt"


def _use_cookie_file(path: Path) -> None:
    config.save({"cookies_file": str(path), "cookies_browser": "", "cookies_profile": "",
                 "cookies_container": ""})


@app.post("/api/cookies/detect")
async def api_cookies_detect():
    return await asyncio.to_thread(cookies.autodetect)


@app.post("/api/cookies/login")
async def api_cookies_login(body: dict = Body(default={})):
    url = str((body or {}).get("url") or "https://www.instagram.com/accounts/login/").strip()
    if not _HTTP_URL.match(url):
        raise _bad("url", "Use a web address that starts with https://")
    return await asyncio.to_thread(cookies.start_login_browser, config.DATA_ROOT, url)


@app.post("/api/cookies/harvest")
async def api_cookies_harvest():
    dest = _cookie_jar()
    result = await asyncio.to_thread(cookies.harvest_login_cookies, dest)
    if result.get("ok"):
        _use_cookie_file(dest)
    return result


@app.post("/api/cookies/import")
def api_cookies_import(body: dict = Body(...)):
    body = body or {}
    dest = _cookie_jar()
    try:
        result = cookies.import_text(str(body.get("text") or ""), dest,
                                     site=str(body.get("site") or body.get("url") or ""))
    except ValueError as exc:
        field = "site" if "site" in str(exc).lower() else "text"
        raise _bad(field, str(exc))
    _use_cookie_file(dest)
    return result


@app.post("/api/cookies/clear")
async def api_cookies_clear():
    """Sign out: stop using any saved sign-in and delete what we stored,
    including the sign-in window's browser profile."""
    removed = await asyncio.to_thread(cookies.forget, config.DATA_ROOT, _cookie_jar())
    config.save({"cookies_file": "", "cookies_browser": "", "cookies_profile": "",
                 "cookies_container": ""})
    return {"ok": True, **removed}


# ---------------------------------------------------------------------- jobs

class JobRequest(BaseModel):
    url: str = ""
    kind: str = "download"                 # download | transcript | live
    options: dict = {}
    hints: dict = {}                       # url -> {title, thumbnail, uploader}


_RUNNERS = {"download": (media.run_download, "work"),
            "transcript": (transcribe.run_transcript, "work"),
            "live": (live.run_live, "live")}


def split_links(text: str) -> tuple[list[str], int]:
    """Links in pasted text: split on whitespace only (commas are legal in
    URLs), keep http(s) links, drop duplicates. Returns (links, ignored)."""
    links: list[str] = []
    ignored = 0
    for token in re.split(r"\s+", text or ""):
        if not token:
            continue
        if _HTTP_URL.match(token) and len(token) <= 4096:
            if token not in links:
                links.append(token)
        else:
            ignored += 1
    return links, ignored


def _start(kind: str, url: str, opts: dict, hint: dict | None, title: str = "") -> dict:
    fn, pool = _RUNNERS[kind]
    job_opts = dict(opts)
    job = jobs.create(kind, url, job_opts, title=title,
                      hints=hint if isinstance(hint, dict) else None)
    jobs.submit(job["id"], fn, url, job_opts, pool=pool)
    return job


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.all_jobs(), "version": jobs.version()}


@app.post("/api/jobs")
def api_create(req: JobRequest):
    if req.kind not in _RUNNERS:
        raise _bad("kind", "Unknown job type.")
    urls, ignored = split_links(req.url)
    if not urls:
        detail = errors.entry("bad_link", "", req.kind)
        detail["ignored"] = ignored
        raise HTTPException(400, detail=detail)
    opts = _clean_options(req.options, req.kind)
    hints = req.hints if isinstance(req.hints, dict) else {}
    created = [_start(req.kind, url, opts, hints.get(url)) for url in urls]
    return {"jobs": created, "ignored": ignored}


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_upload_name(filename: str | None) -> str:
    """The uploaded file's own name, reduced to a plain file name: no folders,
    no drive, nothing Windows refuses. The copy goes into a fresh folder of
    its own, so the name never has to be unique."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + dot + ext[:9]) if dot and len(ext) <= 9 else name[:150]
    return name or "upload"


@app.post("/api/transcribe-file")
async def api_transcribe_file(file: UploadFile = File(...), options: str = Form("{}")):
    try:
        raw = json.loads(options or "{}")
    except ValueError:
        raise _bad("options", "Options must be JSON.")
    opts = _clean_options(raw, "transcript")
    name = safe_upload_name(file.filename)
    folder = jobs.UPLOAD_ROOT / uuid.uuid4().hex
    dest = folder / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            while chunk := await file.read(1 << 20):
                fh.write(chunk)
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, detail=errors.classify(exc, "", "transcript"))
    finally:
        await file.close()
    opts["local_path"] = str(dest)
    opts["local_name"] = name
    # The card shows the name the user knows; only the copy on disk uses the
    # cleaned one.
    shown = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    shown = "".join(c for c in shown if c.isprintable())[:200] or name
    job = jobs.create("transcript", "", opts, title=shown)
    jobs.submit(job["id"], transcribe.run_transcript, "", opts)
    return {"jobs": [job]}


def _job_or_404(jid: str) -> dict:
    job = jobs.get(jid, slim=True)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    return _job_or_404(jid)


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    return {"cancelled": jobs.cancel(jid)}


@app.post("/api/jobs/{jid}/retry")
def api_retry(jid: str, body: dict = Body(default={})):
    """Try again in place, with the same link and options. A patch such as
    {"options": {"vad": false}} backs the 'Try again without it' buttons."""
    job = _job_or_404(jid)
    patch = _clean_options((body or {}).get("options"), job["kind"])
    local = job["options"].get("local_path")
    if job["kind"] == "transcript" and local and not Path(local).is_file():
        raise HTTPException(400, detail=errors.entry(
            "unreadable_file", "", "transcript",
            "The copy of this file is gone. Choose the file again."))
    fresh = jobs.retry(jid, patch or None)
    if fresh is None:
        raise HTTPException(409, "This job is still running.")
    return {"job": fresh}


@app.delete("/api/jobs/{jid}")
def api_remove(jid: str):
    _job_or_404(jid)
    if not jobs.remove(jid):
        raise HTTPException(409, "This job is still running.")
    return {"removed": True}


@app.post("/api/jobs/clear")
@app.post("/api/jobs/clear-completed")
def api_clear():
    return {"removed": jobs.clear_completed()}


@app.post("/api/jobs/restore")
def api_restore():
    return {"restored": jobs.restore()}


# --------------------------------------------------------------- transcripts

def _segments(rows) -> list:
    names = {f.name for f in dataclasses.fields(subs.Segment)}
    out = []
    for s in rows or []:
        if isinstance(s, dict):
            try:
                out.append(subs.Segment(**{k: v for k, v in s.items() if k in names}))
            except TypeError:
                continue
    return out


def _read_sidecar(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sidecar_path(result: dict) -> Path | None:
    stem, outdir = result.get("stem"), result.get("output_dir")
    if not stem or not outdir:
        return None
    return Path(outdir) / f"{stem}.mt.json"


def _job_transcript(jid: str) -> tuple[list, dict, dict]:
    """(segments, meta, result) for a transcript job, from memory or, for a
    job restored from history, from its sidecar file."""
    full = jobs.full_result(jid)
    if full and full.get("segments"):
        return _segments(full["segments"]), full.get("meta") or {}, full
    job = jobs.get(jid, slim=True)
    result = (job or {}).get("result") or {}
    side = _sidecar_path(result)
    data = _read_sidecar(side) if side else None
    if data and data.get("segments"):
        return _segments(data["segments"]), data.get("meta") or result.get("meta") or {}, result
    raise HTTPException(404, "No transcript on that job")


def _render(segs: list, fmt: str, meta: dict, chunk_size: int):
    if fmt not in subs.FORMATTERS:
        raise _bad("format", "Unknown format.")
    if not segs:
        raise HTTPException(404, "No segments")
    body = subs.render(segs, fmt, meta)
    if chunk_size:
        return JSONResponse({"chunks": subs.chunk(body, max(200, chunk_size))})
    return PlainTextResponse(body)


@app.get("/api/jobs/{jid}/transcript")
def api_transcript(jid: str, format: str = "txt", chunk_size: int = 0):
    segs, meta, _ = _job_transcript(jid)
    return _render(segs, format, meta, chunk_size)


def _output_stem(job: dict, result: dict) -> tuple[Path, str]:
    outdir = result.get("output_dir")
    stem = result.get("stem")
    if not stem:
        txt = next((f for f in job.get("files", []) if f.get("ext") == "txt"), None)
        if txt:
            stem, outdir = Path(txt["path"]).stem, outdir or str(Path(txt["path"]).parent)
    if not outdir or not stem:
        raise HTTPException(404, "This transcript has no saved files.")
    return Path(outdir), stem


def _export_format(body: dict | None) -> str:
    fmt = str((body or {}).get("format") or "")
    if fmt not in ("txt", "md", "srt", "vtt", "json"):
        raise _bad("format", "Unknown format.")
    return fmt


def _export_to(outdir: Path, stem: str, fmt: str, segs: list, meta: dict) -> tuple[Path, bool]:
    """Write {stem}.{ext} next to the transcript's other files unless it is
    already there. Returns (path, created)."""
    path = outdir / f"{stem}.{subs.EXTENSIONS.get(fmt, fmt)}"
    if path.exists():
        return path, False
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        path.write_text(subs.render(segs, fmt, meta), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(400, detail=errors.classify(exc, "", "transcript"))
    return path, True


def _edited_text(body: dict | None) -> str:
    text = (body or {}).get("text")
    if not isinstance(text, str):
        raise _bad("text", "Nothing to save.")
    if len(text) > 50 * 1024 * 1024:
        raise _bad("text", "That's too long to save.")
    return text


def _replace_text(path: Path, text: str) -> None:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise HTTPException(400, detail=errors.classify(exc, "", "transcript"))


@app.post("/api/jobs/{jid}/export")
def api_export(jid: str, body: dict = Body(...)):
    """Save another format next to the transcript's other files (Save as)."""
    fmt = _export_format(body)
    job = _job_or_404(jid)
    segs, meta, result = _job_transcript(jid)
    outdir, stem = _output_stem(job, result)
    path, created = _export_to(outdir, stem, fmt, segs, meta)
    jobs.add_file(jid, str(path))
    return {"path": str(path), "created": created}


@app.post("/api/jobs/{jid}/save-text")
def api_save_text(jid: str, body: dict = Body(...)):
    """Keep the user's edits: rewrite the transcript's .txt."""
    text = _edited_text(body)
    job = _job_or_404(jid)
    if job["kind"] != "transcript" or job["status"] != "done":
        raise HTTPException(409, "Only a finished transcript can be edited.")
    outdir, stem = _output_stem(job, job.get("result") or {})
    path = outdir / f"{stem}.txt"
    _replace_text(path, text)
    jobs.add_file(jid, str(path))
    return {"path": str(path)}


def _transcript_dir() -> Path:
    return Path(config.get()["transcript_dir"])


def _sidecar_row(path: Path) -> dict | None:
    data = _read_sidecar(path)
    if not data:
        return None
    meta, detail, stats = data.get("meta") or {}, data.get("detail") or {}, data.get("stats") or {}
    url = meta.get("url") or ""
    stem = path.name[:-len(".mt.json")]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    created = data.get("created")
    # The file "Show file" reveals: the text if it was saved, else whatever was.
    shown = path.with_name(stem + ".txt")
    if not shown.exists():
        for ext in ("md", "srt", "vtt", "json"):
            if path.with_name(f"{stem}.{ext}").exists():
                shown = path.with_name(f"{stem}.{ext}")
                break
    return {"stem": stem, "title": meta.get("title") or stem, "url": url,
            "site": errors.site_name(url) if url else "", "uploader": meta.get("uploader", ""),
            "duration": meta.get("duration"),
            "date": created if isinstance(created, (int, float)) else mtime,
            "words": stats.get("words"), "tokens": stats.get("tokens"),
            "source": detail.get("source", ""), "stats": stats, "detail": detail,
            "path": str(shown)}


@app.get("/api/transcripts")
def api_transcripts(limit: int = 8):
    """Recent transcripts on disk, newest first, read from their sidecars."""
    folder = _transcript_dir()
    try:
        found = sorted(folder.glob("*.mt.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        found = []
    rows = []
    for path in found[:max(1, min(limit, 200))]:
        row = _sidecar_row(path)
        if row:
            rows.append(row)
    return {"transcripts": rows, "total": len(found)}


def _sidecar_by_stem(stem: str) -> Path:
    if not stem or stem != Path(stem).name or stem in (".", "..") or "\\" in stem or "/" in stem:
        raise HTTPException(404, "No such transcript")
    folder = _transcript_dir().resolve()
    path = (folder / f"{stem}.mt.json").resolve()
    if path.parent != folder or not path.is_file():
        raise HTTPException(404, "No such transcript")
    return path


@app.get("/api/transcripts/{stem}")
def api_transcript_file(stem: str, format: str = "txt", chunk_size: int = 0):
    data = _read_sidecar(_sidecar_by_stem(stem)) or {}
    return _render(_segments(data.get("segments")), format, data.get("meta") or {}, chunk_size)


# A transcript from 'Recent transcripts' whose job is gone (cleared, or older
# than the history) still offers Save as and Save changes, by its stem.

@app.post("/api/transcripts/{stem}/export")
def api_transcript_file_export(stem: str, body: dict = Body(...)):
    fmt = _export_format(body)
    side = _sidecar_by_stem(stem)
    data = _read_sidecar(side) or {}
    segs = _segments(data.get("segments"))
    if not segs:
        raise HTTPException(404, "No segments")
    path, created = _export_to(side.parent, stem, fmt, segs, data.get("meta") or {})
    return {"path": str(path), "created": created}


@app.post("/api/transcripts/{stem}/save-text")
def api_transcript_file_save_text(stem: str, body: dict = Body(...)):
    text = _edited_text(body)
    path = _sidecar_by_stem(stem).with_name(f"{stem}.txt")
    _replace_text(path, text)
    return {"path": str(path)}


# -------------------------------------------------------- files on the disk

# What Play and Open hand to the shell: the kinds of file the app itself
# writes (media, subtitles, transcripts, pictures, page shortcuts) and its
# own text files (the log, the licence notices). An allowlist, because
# Windows runs far more file types than any list of dangerous ones names.
# "1" is the rotated app.log.1; "xml" covers srv3 and TTML subtitles.
_OPEN_OK = ({ext for kind, exts in jobs._EXT_KIND.items() if kind != "part" for ext in exts}
            - {"desktop", "webloc"}) | {"log", "1", "xml"}


def _lex(path: str) -> str:
    """Lexical, case-folded absolute form. Never touches the file system, so
    a UNC path is judged before Windows tries to reach that server."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _inside(lex_path: str, lex_root: str) -> bool:
    root = lex_root.rstrip("\\/")
    return lex_path == root or lex_path.startswith(root + os.sep)


def _allowed() -> tuple[list[str], set[str]]:
    """Folders the app may open things in, and files jobs recorded (so 'Show
    in folder' still works after the user changes their folders)."""
    cfg = config.get()
    roots = [cfg["download_dir"], cfg["transcript_dir"], str(config.DATA_ROOT), str(config.ROOT)]
    files: set[str] = set()
    for job in jobs.all_jobs(slim=True):
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        if result.get("output_dir"):
            roots.append(str(result["output_dir"]))
        for f in job.get("files") or []:
            if f.get("path"):
                files.add(str(f["path"]))
    return [r for r in roots if r], files


def confined(path: str) -> Path:
    """The real path, if it lies inside an allowed folder; otherwise HTTP 403
    (or 404 when it does not exist)."""
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise HTTPException(400, "No path")
    roots, files = _allowed()
    lex = _lex(raw)
    lex_files = {_lex(f) for f in files}
    lex_roots = [_lex(r) for r in roots]
    if lex not in lex_files and not any(_inside(lex, r) for r in lex_roots):
        raise HTTPException(403, "That is outside the download and transcript folders.")
    try:
        real = Path(raw).resolve(strict=True)
    except OSError:
        raise HTTPException(404, "That file or folder isn't there any more.")
    # Links inside an allowed folder must not lead back out of it.
    real_lex = os.path.normcase(str(real))
    ok = real_lex in {os.path.normcase(str(Path(f).resolve())) for f in files if _lex(f) == lex}
    if not ok:
        for r in roots:
            try:
                root_real = os.path.normcase(str(Path(r).resolve()))
            except OSError:
                continue
            if _inside(real_lex, root_real):
                ok = True
                break
    if not ok:
        raise HTTPException(403, "That is outside the download and transcript folders.")
    return real


def _link_target_ok(path: Path) -> bool:
    """A saved page shortcut may only point at a web page."""
    try:
        text = path.read_text("utf-8", errors="replace")[:4096]
    except OSError:
        return False
    m = re.search(r"(?im)^\s*URL\s*=\s*(\S+)", text)
    return bool(m and _HTTP_URL.match(m.group(1)))


def _shell_open(target: Path) -> None:
    if os.name == "nt":
        os.startfile(str(target))                       # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


@app.post("/api/open")
def api_open(body: dict = Body(...)):
    """Open a file with its default app (Play, Open) or a folder in Explorer."""
    target = confined((body or {}).get("path", ""))
    if target.is_file():
        if target.suffix.lower().lstrip(".") not in _OPEN_OK:
            raise HTTPException(403, "Media Toolkit only opens the media and text files it "
                                     "saves. Use Show in folder instead.")
        if target.suffix.lower() == ".url" and not _link_target_ok(target):
            raise HTTPException(403, "That shortcut doesn't point to a web page.")
    try:
        _shell_open(target)
    except OSError as exc:
        raise HTTPException(500, f"Couldn't open it: {exc.strerror or exc}")
    return {"ok": True}


@app.post("/api/reveal")
def api_reveal(body: dict = Body(...)):
    """Show a file selected in its folder, or open a folder."""
    target = confined((body or {}).get("path", ""))
    try:
        if os.name == "nt":
            if target.is_dir():
                os.startfile(str(target))               # noqa: S606
            else:
                # One verbatim string: Explorer splits its own command line
                # at commas, so the path must always be quoted, and a list
                # only gets quotes when the path has a space. Windows paths
                # cannot contain a double quote.
                subprocess.Popen(f'explorer.exe /select,"{target}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open"] + (["-R"] if target.is_file() else []) + [str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
    except OSError as exc:
        raise HTTPException(500, f"Couldn't open the folder: {exc.strerror or exc}")
    return {"ok": True}


# Pages the app itself links to (Settings health banner, About, updates):
# host -> path prefix ("" = the whole site).
_OPEN_URL_SITES = {"nodejs.org": "", "github.com": "/anotherah/media-toolkit",
                   # NVIDIA's licence, shown before the GPU support download.
                   "docs.nvidia.com": "/cuda/eula"}


def _own_page(url: str) -> bool:
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme.lower() != "https" or p.username or p.password or p.port not in (None, 443):
        return False
    if host == "www.nodejs.org":
        host = "nodejs.org"
    if host not in _OPEN_URL_SITES:
        return False
    prefix = _OPEN_URL_SITES[host]
    path = p.path.lower().rstrip("/")
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _norm_url(url: str) -> tuple | None:
    """Comparable form of a media link: time offsets and fragments ignored."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if k not in ("t", "start", "time_continue"))
    return host, p.path.rstrip("/"), urlencode(query)


def url_allowed(url: str, stem: str = "") -> bool:
    url = (url or "").strip()
    if not _HTTP_URL.match(url):
        return False
    if _own_page(url):
        return True
    wanted = _norm_url(url)
    if wanted is None:
        return False
    known: list[str] = []
    for job in jobs.all_jobs(slim=True):
        known.append(job.get("url") or "")
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        meta = result.get("meta") or {}
        known += [meta.get("url") or "", meta.get("webpage_url") or ""]
    if stem:
        try:
            data = _read_sidecar(_sidecar_by_stem(stem)) or {}
            known.append((data.get("meta") or {}).get("url") or "")
        except HTTPException:
            pass
    return any(k and _norm_url(k) == wanted for k in known)


@app.post("/api/open-url")
def api_open_url(body: dict = Body(...)):
    """Open a web page in the user's normal browser: the app's own help
    pages, or the source of a transcript at a given moment."""
    body = body or {}
    url = str(body.get("url") or "")
    if not url_allowed(url, str(body.get("stem") or "")):
        raise HTTPException(403, "That address can't be opened from here.")
    import webbrowser
    webbrowser.open(url)
    return {"ok": True}


# ----------------------------------------------------------------------- SSE

# Liveness. The UI posts a heartbeat every few seconds; the launcher quits
# once those stop. A positive signal beats inferring a closed window from a
# dropped SSE stream, which uvicorn may not notice until its next write.
#
# goodbye (sent as the page unloads) no longer ends things by itself: a
# reload sends goodbye from the old page and heartbeats from the new one in
# either order, so the launcher only treats it as a close when no heartbeat
# or new event stream follows it within a few seconds.
_lock = threading.Lock()
_state = {"clients": 0, "last_seen": time.time(), "last_beat": 0.0,
          "seen_any": False, "goodbye_at": 0.0}


def liveness() -> dict:
    with _lock:
        return dict(_state)


def client_state() -> tuple[int, float, bool]:
    """The 1.1 launcher's view: (clients, last_seen, seen_any)."""
    s = liveness()
    return s["clients"], s["last_seen"], s["seen_any"]


def _alive(now: float | None = None) -> None:
    now = now or time.time()
    _state["last_seen"] = _state["last_beat"] = now
    _state["seen_any"] = True
    if _state["goodbye_at"] and _state["goodbye_at"] < now:
        _state["goodbye_at"] = 0.0


@app.post("/api/heartbeat")
def api_heartbeat():
    with _lock:
        _alive()
    return {"ok": True, "active": jobs.active_count()}


@app.post("/api/goodbye")
def api_goodbye():
    with _lock:
        _state["goodbye_at"] = time.time()
    return {"ok": True}


@app.get("/api/events")
async def api_events():
    with _lock:
        _state["clients"] += 1
        _alive()

    async def stream():
        last = -1
        idle = 0
        try:
            while True:
                current = jobs.version()
                if current != last:
                    last = current
                    payload = json.dumps({"jobs": jobs.all_jobs(slim=True), "version": current},
                                         ensure_ascii=False, default=str)
                    yield f"data: {payload}\n\n"
                    idle = 0
                else:
                    idle += 1
                    if idle >= 30:        # keepalive so proxies do not close us
                        idle = 0
                        yield ": ping\n\n"
                with _lock:
                    _state["last_seen"] = time.time()
                await asyncio.sleep(0.4)
        finally:
            with _lock:
                _state["clients"] -= 1
                _state["last_seen"] = time.time()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
