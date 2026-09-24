"""Paths, persisted settings, and native-library bootstrap."""
from __future__ import annotations

import json
import os
import re
import site
import sys
import time
from pathlib import Path
from threading import Lock

FROZEN = getattr(sys, "frozen", False)


def _usable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


if FROZEN:
    # PyInstaller: read-only payload sits next to the exe, user data must not.
    ROOT = Path(sys.executable).resolve().parent
    if os.name == "nt":
        DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Media Toolkit"
    else:
        DATA_ROOT = Path.home() / ".local/share/media-toolkit"
    # Portable mode: a portable.txt beside the exe keeps every byte of user
    # data on the same stick, so the app leaves nothing behind on the PC. A
    # copy unpacked somewhere read-only silently keeps the normal location.
    if (ROOT / "portable.txt").exists() and _usable(ROOT / "data"):
        DATA_ROOT = ROOT / "data"
else:
    ROOT = Path(__file__).resolve().parents[1]
    DATA_ROOT = ROOT

# An explicit data folder wins over everything: lets several copies run side
# by side without sharing settings, and lets tests start from a clean slate.
if os.environ.get("MEDIA_TOOLKIT_HOME"):
    DATA_ROOT = Path(os.environ["MEDIA_TOOLKIT_HOME"]).expanduser().resolve()

PORTABLE = FROZEN and DATA_ROOT == ROOT / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
BIN_DIR = ROOT / "bin"
RUNTIME_DIR = DATA_ROOT / "runtime"        # on-demand packs (CUDA, ffmpeg)
CUDA_DIR = RUNTIME_DIR / "cuda"
CONFIG_PATH = DATA_ROOT / "config.json"
BACKUP_PATH = DATA_ROOT / "config.json.bak"
LOG_PATH = DATA_ROOT / "app.log"

# File-name presets (Settings > Downloads > File names). "custom" keeps
# whatever output_template holds.
FILENAME_PRESETS: dict[str, str] = {
    "title_id": "%(title).180B [%(id)s].%(ext)s",
    "title": "%(title).200B.%(ext)s",
    "channel_title": "%(uploader,channel|Unknown).60B - %(title).150B.%(ext)s",
    "date_title": "%(upload_date>%Y-%m-%d|)s %(title).180B.%(ext)s",
}

DEFAULTS: dict = {
    "setup_complete": False,        # drives the first-run wizard
    "download_dir": str(DATA_ROOT / "downloads"),
    "transcript_dir": str(DATA_ROOT / "transcripts"),
    # --- authentication -----------------------------------------------------
    "cookies_browser": "",          # "", chrome, firefox, edge, brave, opera, vivaldi, chromium
    "cookies_profile": "",          # optional browser profile name
    "cookies_container": "",        # Firefox container
    "cookies_file": "",             # path to a cookies.txt
    # --- network ------------------------------------------------------------
    "concurrent_fragments": 8,
    "proxy": "",
    "rate_limit": "",               # e.g. "5M"
    "impersonate": "",              # "", or a browser family such as "chrome"
    "force_ipv4": False,
    "geo_bypass_country": "",       # two-letter ISO code
    "user_agent": "",
    "referer": "",
    "sleep_requests": 0.0,
    "sleep_interval": 0.0,
    "max_sleep_interval": 0.0,
    "external_downloader": "",      # "" or "aria2c", nothing else
    "throttled_rate": "",           # re-extract below this speed, e.g. "100K"
    # --- files --------------------------------------------------------------
    "embed_metadata": True,
    "embed_thumbnail": True,
    "embed_chapters": True,
    "write_info_json": False,
    "write_description": False,
    "write_comments": False,
    "convert_thumbnails": "",       # "", jpg, png
    "set_mtime": False,             # off: new files sort to the top in Explorer
    "use_temp_dir": True,           # stage in temp, move on completion
    "sponsorblock": False,
    "sponsorblock_mode": "remove",  # remove | mark
    "sponsorblock_categories": "sponsor,selfpromo,interaction",   # comma separated
    "restrict_filenames": False,
    "filename_preset": "title_id",  # title_id | title | channel_title | date_title | custom
    "output_template": FILENAME_PRESETS["title_id"],
    # --- encoding -----------------------------------------------------------
    "recode_encoder": "",           # "" = never re-encode (remux only)
    "recode_quality": "balanced",
    "normalize_audio": False,
    # --- whisper ------------------------------------------------------------
    "whisper_model": "large-v3-turbo",
    "whisper_device": "auto",       # auto | cuda | cpu
    "whisper_compute": "auto",      # auto | float16 | int8_float16 | int8 | float32
    "whisper_vad": True,
    "whisper_beam": 5,
    "prefer_native_subs": True,
    "subtitle_langs": "en",
    "transcript_language": "",      # "" = same as the video
    # --- remembered Download tab choices -------------------------------------
    "dl_mode": "video",
    "dl_quality": "1080",
    "dl_compatible": True,
    "dl_audio_codec": "mp3",
    "dl_container": "mp4",
    "dl_subtitles": "none",
    "dl_subtitle_langs": "en",
    "dl_auto_subs": False,
    "dl_embed_subs": True,
    "dl_split_chapters": False,
    "dl_max_comments": 200,
    "dl_write_thumbnail": False,
    "dl_write_link": False,
    # --- remembered Live tab choices -----------------------------------------
    "lv_audio": False,
    "lv_quality": "best",
    "lv_container": "mp4",
    "lv_max": "",
    "lv_split": "",
    "lv_wait": False,
    "lv_wait_hours": 3,
    # --- one-time prompts ----------------------------------------------------
    "notify_prompted": False,
}

_lock = Lock()
_cache: dict | None = None

# Set when config.json could not be read at startup. The UI shows it once
# instead of the app silently starting over with default folders.
NOTICE = ""
# Folders from config that could not be created at startup: {key: message}.
FOLDER_PROBLEMS: dict[str, str] = {}


def output_template_ok(template: str) -> bool:
    """A file-name pattern may only name a file (and optional sub-folders)
    inside the download folder: no drive, no absolute path, no '..'."""
    t = (template or "").strip()
    if not t or len(t) > 400:
        return False
    if t.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", t):
        return False
    parts = re.split(r"[\\/]", t)
    return ".." not in [p.strip() for p in parts]


def _sanitise(data: dict) -> dict:
    """Stored values that would be dangerous whoever wrote them: a config
    edited by hand or by an older build is not trusted blindly."""
    out = {k: v for k, v in data.items() if k in DEFAULTS}
    if out.get("external_downloader") not in (None, "", "aria2c"):
        out["external_downloader"] = ""
    if "output_template" in out and not output_template_ok(str(out["output_template"])):
        out["output_template"] = DEFAULTS["output_template"]
        out["filename_preset"] = "title_id"
    # 1.1 configs had no preset: a template of their own is a custom one.
    if "filename_preset" not in data and out.get("output_template") not in (
            None, DEFAULTS["output_template"]):
        out["filename_preset"] = "custom"
    return out


def _load_json(path: Path):
    """The parsed dict, None when the file is absent, or False when it is
    damaged. utf-8-sig so a file saved by Notepad with a BOM still loads."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8-sig"))
        return data if isinstance(data, dict) else False
    except (OSError, ValueError):
        return False


def _read() -> dict:
    global NOTICE
    cfg = dict(DEFAULTS)
    data = _load_json(CONFIG_PATH)
    if data is False:
        # Keep the damaged file for the user and fall back to the last good copy.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            CONFIG_PATH.replace(DATA_ROOT / f"config.corrupt-{stamp}.json")
        except OSError:
            pass
        backup = _load_json(BACKUP_PATH)
        if isinstance(backup, dict):
            data = backup
            NOTICE = "Your settings file was damaged, so the last good copy was loaded."
            try:                      # later reads (and saves) start from it too
                _write_atomic(CONFIG_PATH, json.dumps(backup, indent=2, ensure_ascii=False))
            except OSError:
                pass
        else:
            data = {}
            NOTICE = "Your settings file was damaged, so the default settings were loaded."
        print(f"config: {NOTICE} The damaged file was kept as config.corrupt-{stamp}.json")
    if data:
        cfg.update(_sanitise(data))
    return cfg


def _write_atomic(path: Path, text: str) -> None:
    """Write-then-rename, so a crash or a full disk mid-save can never leave
    a half-written config behind."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def get() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _read()
        return dict(_cache)


def save(patch: dict) -> dict:
    global _cache
    with _lock:
        cfg = _read()
        cfg.update({k: v for k, v in patch.items() if k in DEFAULTS})
        cfg.update(_sanitise(cfg))
        text = json.dumps(cfg, indent=2, ensure_ascii=False)
        _write_atomic(CONFIG_PATH, text)
        try:
            _write_atomic(BACKUP_PATH, text)
        except OSError:
            pass
        _cache = cfg
        return dict(cfg)


def ffmpeg_dir() -> str | None:
    """Directory holding ffmpeg: shipped alongside the app, or downloaded later."""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for d in (BIN_DIR, RUNTIME_DIR / "bin"):
        if (d / name).exists():
            return str(d)
    return None


# Proxy variables this process set itself, so clearing the setting restores
# whatever the user's environment had before.
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_saved_env: dict[str, str | None] | None = None


def apply_proxy_env(proxy: str | None = None) -> None:
    """Route the downloads that do not go through yt-dlp (speech models, the
    GPU and ffmpeg packs, live recording's ffmpeg) through the same proxy.

    Only http(s) proxies are exported: the model downloader cannot speak
    SOCKS without an extra package, and a proxy variable it cannot use breaks
    every download instead of none. Loopback is always exempt so the app can
    still reach itself.
    """
    global _saved_env
    if proxy is None:
        try:
            proxy = get().get("proxy") or ""
        except Exception:
            proxy = ""
    proxy = (proxy or "").strip()
    if _saved_env is None:
        _saved_env = {k: os.environ.get(k) for k in _PROXY_VARS + ("NO_PROXY", "no_proxy")}
    if re.match(r"^https?://\S+$", proxy, re.I):
        for k in _PROXY_VARS:
            os.environ[k] = proxy
        loop = "localhost,127.0.0.1,::1"
        for k in ("NO_PROXY", "no_proxy"):
            prev = _saved_env.get(k)
            os.environ[k] = f"{prev},{loop}" if prev else loop
    else:
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_registered: set[str] = set()        # DLL folders already put on PATH


def bootstrap() -> None:
    """Make bundled ffmpeg and pip-installed CUDA libraries loadable, and keep
    third-party libraries from phoning home or picking up identities."""
    # huggingface_hub: no telemetry or agent detection, never send an access
    # token some other tool left in the user's profile, and keep its caches
    # inside our data folder so deleting that folder removes every trace.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    os.environ.setdefault("HF_HOME", str(DATA_ROOT / "hf"))
    os.environ.setdefault("DO_NOT_TRACK", "1")
    apply_proxy_env()

    if (d := ffmpeg_dir()):
        if d not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

    if os.name != "nt":
        return

    def register(path: Path) -> None:
        # CTranslate2 resolves CUDA libraries with LoadLibrary at call time, which
        # consults PATH; add_dll_directory alone is not enough. bootstrap() runs
        # again after every pack install, so each folder is added only once.
        if str(path) in _registered:
            return
        try:
            os.add_dll_directory(str(path))
        except OSError:
            return
        _registered.add(str(path))
        os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")

    if CUDA_DIR.is_dir():
        register(CUDA_DIR)

    # Development checkout: pick the CUDA libraries straight out of site-packages.
    if not FROZEN:
        roots = set(site.getsitepackages())
        for sp in roots:
            nv = Path(sp) / "nvidia"
            if not nv.is_dir():
                continue
            for sub in nv.glob("*/bin"):
                register(sub)


def ensure_dirs() -> dict[str, str]:
    """Create the working folders. Never raises: an unplugged external drive
    or an offline network share must not stop the app from starting. What
    failed is kept in FOLDER_PROBLEMS for the Settings screen to show."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"config: cannot create {RUNTIME_DIR}: {exc}")
    FOLDER_PROBLEMS.clear()
    cfg = get()
    for key in ("download_dir", "transcript_dir"):
        try:
            Path(cfg[key]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            FOLDER_PROBLEMS[key] = f"Can't use this folder: {folder_reason(exc)}"
            print(f"config: cannot create {key} {cfg[key]!r}: {exc}")
    return dict(FOLDER_PROBLEMS)


def folder_reason(exc: OSError) -> str:
    """Why a folder cannot be used, in words the Settings screen can show."""
    import errno
    win = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError) or win == 5:
        return "access denied"
    if exc.errno == errno.ENOSPC or win in (39, 112):
        return "the drive is full"
    if win in (3, 21, 53, 67, 1231) or isinstance(exc, FileNotFoundError):
        return "the drive or network location isn't available"
    if win == 123 or exc.errno == errno.EINVAL:
        return "the name contains characters Windows doesn't allow"
    if exc.errno == errno.EROFS:
        return "the drive is read-only"
    return (exc.strerror or str(exc)).rstrip(".")
