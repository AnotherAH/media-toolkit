"""Paths, persisted settings, and native-library bootstrap."""
from __future__ import annotations

import json
import os
import site
import sys
from pathlib import Path
from threading import Lock

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # PyInstaller: read-only payload sits next to the exe, user data must not.
    ROOT = Path(sys.executable).resolve().parent
    if os.name == "nt":
        DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Media Toolkit"
    else:
        DATA_ROOT = Path.home() / ".local/share/media-toolkit"
else:
    ROOT = Path(__file__).resolve().parents[1]
    DATA_ROOT = ROOT

DATA_ROOT.mkdir(parents=True, exist_ok=True)
BIN_DIR = ROOT / "bin"
RUNTIME_DIR = DATA_ROOT / "runtime"        # on-demand packs (CUDA, ffmpeg)
CUDA_DIR = RUNTIME_DIR / "cuda"
CONFIG_PATH = DATA_ROOT / "config.json"

DEFAULTS: dict = {
    "setup_complete": False,        # drives the first-run wizard
    "download_dir": str(DATA_ROOT / "downloads"),
    "transcript_dir": str(DATA_ROOT / "transcripts"),
    # --- authentication -----------------------------------------------------
    "cookies_browser": "",          # "", chrome, firefox, edge, brave, opera, vivaldi, chromium
    "cookies_profile": "",          # optional browser profile name
    "cookies_container": "",        # Firefox container
    "cookies_file": "",             # path to a cookies.txt (wins over browser)
    # --- network ------------------------------------------------------------
    "concurrent_fragments": 8,
    "proxy": "",
    "rate_limit": "",               # e.g. "5M"
    "impersonate": "",              # "", "chrome", "safari", "chrome:windows-10", ...
    "force_ipv4": False,
    "geo_bypass_country": "",       # two-letter ISO code
    "user_agent": "",
    "referer": "",
    "sleep_requests": 0.0,
    "sleep_interval": 0.0,
    "max_sleep_interval": 0.0,
    "external_downloader": "",      # "", aria2c
    "throttled_rate": "",           # re-extract below this speed, e.g. "100K"
    # --- files --------------------------------------------------------------
    "embed_metadata": True,
    "embed_thumbnail": True,
    "embed_chapters": True,
    "write_info_json": False,
    "write_description": False,
    "write_comments": False,
    "convert_thumbnails": "",       # "", jpg, png
    "set_mtime": True,
    "use_temp_dir": True,           # stage in temp, move on completion
    "sponsorblock": False,
    "sponsorblock_mode": "remove",  # remove | mark
    "restrict_filenames": False,
    "output_template": "%(title).180B [%(id)s].%(ext)s",
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
}

_lock = Lock()
_cache: dict | None = None


def _read() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception:
            pass
    return cfg


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
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")
        _cache = cfg
        return dict(cfg)


def ffmpeg_dir() -> str | None:
    """Directory holding ffmpeg: shipped alongside the app, or downloaded later."""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for d in (BIN_DIR, RUNTIME_DIR / "bin"):
        if (d / name).exists():
            return str(d)
    return None


def bootstrap() -> None:
    """Make bundled ffmpeg and pip-installed CUDA libraries loadable."""
    if (d := ffmpeg_dir()):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

    if os.name != "nt":
        return

    def register(path: Path) -> None:
        # CTranslate2 resolves CUDA libraries with LoadLibrary at call time, which
        # consults PATH -- add_dll_directory alone is not enough.
        try:
            os.add_dll_directory(str(path))
        except OSError:
            return
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


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for key in ("download_dir", "transcript_dir"):
        Path(get()[key]).mkdir(parents=True, exist_ok=True)
