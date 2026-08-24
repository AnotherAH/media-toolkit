"""In-memory job registry with a worker pool.

yt-dlp and faster-whisper are both blocking, so work runs on threads and the UI
polls a version counter over SSE. Small surface on purpose: create, snapshot,
update, cancel.
"""
from __future__ import annotations

import itertools
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Event
from typing import Any, Callable

_ids = itertools.count(1)
_lock = Lock()
_jobs: dict[str, dict] = {}
_cancels: dict[str, Event] = {}
_version = 0

POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="worker")


class Cancelled(Exception):
    """Raised inside a worker when the user cancels the job."""


def _bump() -> None:
    global _version
    _version += 1


def version() -> int:
    return _version


def create(kind: str, url: str, options: dict | None = None, title: str = "") -> dict:
    jid = f"j{next(_ids)}"
    job = {
        "id": jid,
        "kind": kind,                 # download | transcript | both
        "url": url,
        "title": title or url,
        "status": "queued",           # queued running done error cancelled
        "stage": "Queued",
        "progress": 0.0,
        "speed": "",
        "eta": "",
        "message": "",
        "options": options or {},
        "files": [],
        "result": None,
        "thumbnail": "",
        "created": time.time(),
        "updated": time.time(),
    }
    with _lock:
        _jobs[jid] = job
        _cancels[jid] = Event()
        _bump()
    return dict(job)


def update(jid: str, **fields) -> None:
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        job.update(fields)
        job["updated"] = time.time()
        _bump()


def add_file(jid: str, path: str, label: str = "") -> None:
    import os
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        if any(f["path"] == path for f in job["files"]):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        job["files"].append({"path": path, "name": os.path.basename(path),
                             "label": label, "size": size})
        job["updated"] = time.time()
        _bump()


def get(jid: str) -> dict | None:
    with _lock:
        job = _jobs.get(jid)
        return dict(job) if job else None


def all_jobs() -> list[dict]:
    with _lock:
        return [dict(j) for j in sorted(_jobs.values(), key=lambda j: -j["created"])]


def cancel(jid: str) -> bool:
    with _lock:
        ev = _cancels.get(jid)
        job = _jobs.get(jid)
        if not ev or not job or job["status"] in ("done", "error", "cancelled"):
            return False
        ev.set()
        job["status"] = "cancelled"
        job["stage"] = "Cancelled"
        job["updated"] = time.time()
        _bump()
        return True


def clear_cancel(jid: str) -> None:
    """A live recording that the user stopped is finished, not failed -- clear the
    flag so the job lands on "done" with its file attached."""
    ev = _cancels.get(jid)
    if ev:
        ev.clear()


def cancelled(jid: str) -> bool:
    ev = _cancels.get(jid)
    return bool(ev and ev.is_set())


def raise_if_cancelled(jid: str) -> None:
    if cancelled(jid):
        raise Cancelled()


def active_count() -> int:
    """Jobs still doing real work. The launcher refuses to quit while any exist,
    so closing the window can never abandon a download or a recording."""
    with _lock:
        return sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))


def clear_finished() -> int:
    with _lock:
        gone = [k for k, j in _jobs.items() if j["status"] in ("done", "error", "cancelled")]
        for k in gone:
            _jobs.pop(k, None)
            _cancels.pop(k, None)
        _bump()
        return len(gone)


def submit(jid: str, fn: Callable[..., Any], *args, **kwargs) -> None:
    """Run fn(jid, ...) on the pool, translating outcomes into job status."""
    def runner():
        if cancelled(jid):
            return
        update(jid, status="running", stage="Starting", message="")
        try:
            result = fn(jid, *args, **kwargs)
            if cancelled(jid):
                update(jid, status="cancelled", stage="Cancelled")
            else:
                update(jid, status="done", stage="Done", progress=1.0,
                       result=result, speed="", eta="")
        except Cancelled:
            update(jid, status="cancelled", stage="Cancelled")
        except Exception as exc:
            update(jid, status="error", stage="Failed",
                   message=_friendly(exc), speed="", eta="")
            traceback.print_exc()

    POOL.submit(runner)


def _friendly(exc: Exception) -> str:
    """Turn the noisiest known failures into something a human can act on."""
    msg = str(exc).strip() or exc.__class__.__name__
    low = msg.lower()
    hints = [
        ("empty media response", "Instagram will not serve this post anonymously. Open Settings, set "
                                 "\"Take cookies from browser\" to a browser you are logged into "
                                 "(Firefox is the most reliable on Windows), then try again."),
        ("cookies-from-browser", "This post needs a logged-in session. Open Settings and set "
                                 "\"Take cookies from browser\", then try again."),
        ("login required", "This post is private or age-gated. Pick your browser under Settings > Cookies and try again."),
        ("rate-limit", "The site is rate-limiting you. Wait a few minutes, or set a proxy in Settings."),
        ("sign in to confirm", "The site wants a logged-in session. Set Cookies to your browser in Settings."),
        ("could not copy chrome cookie database", "Chrome locks its cookie file while running. Close Chrome, "
                                                  "or switch to Firefox in Settings."),
        ("failed to decrypt", "Chrome and Edge encrypt cookies on Windows so yt-dlp cannot read them. "
                              "Use Firefox, or export a cookies.txt file and point to it in Settings."),
        ("unable to extract", "The extractor for this site needs an update. Run Update yt-dlp in Settings."),
        ("unsupported url", "No extractor matched that link. Check the URL, or run Update yt-dlp."),
        # Specific causes first: "cuda" appears in every backend label, so a
        # broad match on it hides the real reason.
        ("unable to open file", "The Whisper model files are incomplete. They will be re-downloaded "
                                "automatically the next time you transcribe."),
        ("did not download correctly", "The model download was interrupted. Try again -- it resumes "
                                       "from scratch and reports progress."),
        ("cudnn", "cuDNN could not be loaded, so the GPU path is unavailable. Set Device to CPU in Settings."),
        ("cuda driver", "The GPU driver rejected the request; the app will fall back to CPU."),
        ("cuda failed", "GPU transcription failed; retry and it will fall back to CPU automatically."),
        ("out of memory", "The model is too big for this GPU. Pick a smaller model in Settings."),
        ("ffmpeg", "ffmpeg is missing or failed. Re-run setup to restore it in the bin folder."),
    ]
    for needle, hint in hints:
        if needle in low:
            return f"{hint}\n\nDetail: {msg[:400]}"
    return msg[:600]
