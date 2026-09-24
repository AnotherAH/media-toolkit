"""In-memory job registry with a worker pool, plus a small on-disk history.

yt-dlp and faster-whisper are both blocking, so work runs on threads and the UI
follows a version counter over SSE. Downloads and transcripts share a pool of
three workers; live recordings and waits get a daemon thread each, because an
overnight recording must never hold a slot that a two-second caption fetch is
waiting for.

A job's worker may still be running after the user cancelled it (ffmpeg is
finalising, yt-dlp is between progress ticks). Until it returns, the job is
never dropped from the registry, so its cancel flag cannot be lost and the
launcher keeps counting it as work in progress.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable

from . import config, errors

try:                                             # yt-dlp re-raises its own
    from yt_dlp.utils import DownloadCancelled as _CancelBase   # cancellation even
except Exception:                                # with ignoreerrors, so ours must
    _CancelBase = Exception                      # be one of them


class Cancelled(_CancelBase):
    """Raised inside a worker when the user cancels the job."""
    msg = "Cancelled"


class Skipped(Exception):
    """Nothing was produced on purpose (every item filtered out, or already
    downloaded). ``reason`` is plain text and becomes the job's stage."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


ACTIVE = ("queued", "running", "stopping")
FINAL = ("done", "skipped", "error", "cancelled")
CLEARABLE = ("done", "skipped", "cancelled")
STEP_STATES = ("pending", "active", "done", "skipped", "failed")
FILE_KINDS = ("video", "audio", "transcript", "subtitles", "notes", "json", "thumbnail",
              "description", "link", "part", "other")
HISTORY_LIMIT = 200
RESTORE_SECONDS = 10.0
# Uploads for local-file transcripts live here, one folder per upload.
UPLOAD_ROOT = Path(tempfile.gettempdir()) / "media-toolkit" / "uploads"

_ids = itertools.count(1)
_lock = Lock()
_hist_lock = Lock()
_jobs: dict[str, dict] = {}
_cancels: dict[str, Event] = {}
_running: set[str] = set()               # submitted, worker not returned yet
_started: set[str] = set()               # ...and the worker has actually begun
_recipes: dict[str, tuple] = {}          # jid -> (fn, args, kwargs, pool) for retry
_runners: dict[str, tuple[Callable, str]] = {}   # kind -> (fn, pool) for history retries
_trash: list[tuple[float, list[dict]]] = []      # cleared jobs, restorable briefly
_version = 0

POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="worker")

_EXT_KIND = {
    "video": ("mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "ts", "3gp", "wmv", "mpg"),
    "audio": ("mp3", "m4a", "opus", "flac", "wav", "ogg", "aac", "mka", "oga", "weba", "wma",
              "alac", "vorbis"),
    "transcript": ("txt",),
    "subtitles": ("srt", "vtt", "ass", "ssa", "lrc", "ttml", "sbv"),
    "notes": ("md",),
    "json": ("json",),
    "thumbnail": ("jpg", "jpeg", "png", "webp", "gif", "avif"),
    "description": ("description",),
    "link": ("url", "webloc", "desktop"),
    "part": ("part", "ytdl"),
}


def _bump() -> None:
    global _version
    _version += 1


def version() -> int:
    return _version


def register(kind: str, fn: Callable[..., Any], pool: str = "work") -> None:
    """Tell the registry which worker runs a kind, so a job restored from a
    previous session can still be retried."""
    _runners[kind] = (fn, pool)


def _new_job(jid: str, kind: str, url: str, options: dict, title: str, hints: dict) -> dict:
    now = time.time()
    return {
        "id": jid,
        "kind": kind,                 # download | transcript | live
        "url": url,
        "title": title or hints.get("title") or url,
        "thumbnail": hints.get("thumbnail") or "",
        "uploader": hints.get("uploader") or "",
        "status": "queued",
        "stage": "Waiting",
        "stage_detail": "",
        "progress": 0.0,
        "indeterminate": False,
        "bytes_done": None,
        "bytes_total": None,
        "speed_bps": None,
        "eta_s": None,
        "speed": "",
        "eta": "",
        "item": None,
        "steps": [],
        "live_phase": None,
        "rec_seconds": None,
        "rec_bytes": None,
        "rec_limit_seconds": None,
        "next_check_at": None,
        "give_up_at": None,
        "parts": None,
        "error": None,
        "message": "",
        "files": [],
        "result": None,
        "options": dict(options or {}),
        "created": now,
        "updated": now,
        "from_history": False,
    }


def create(kind: str, url: str, options: dict | None = None, title: str = "",
           hints: dict | None = None) -> dict:
    """Register a queued job. ``hints`` ({title, thumbnail, uploader}) come
    from the link preview, so a card never shows a bare URL while it waits."""
    # Hints come from the request: plain short text, and a thumbnail only as
    # a web address, since the page puts it straight into an <img>.
    hints = {k: v.strip()[:500] for k, v in (hints or {}).items()
             if k in ("title", "thumbnail", "uploader") and isinstance(v, str)}
    if hints.get("thumbnail") and not re.match(r"^https?://\S+$", hints["thumbnail"], re.I):
        hints.pop("thumbnail")
    with _lock:
        jid = f"j{next(_ids)}"
        while jid in _jobs:
            jid = f"j{next(_ids)}"
        job = _new_job(jid, kind, url, options or {}, title, hints)
        _jobs[jid] = job
        _cancels[jid] = Event()
        _bump()
        return _snapshot(job, slim=True)


def update(jid: str, **fields) -> None:
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        # A bar never moves backwards within one phase: engines report
        # per-stream fractions, and a late tick must not undo a newer one.
        if "progress" in fields and job["status"] == "running" and \
                fields.get("stage", job["stage"]) == job["stage"] and \
                fields.get("progress") is not None and job["progress"] is not None and \
                fields["progress"] < job["progress"]:
            fields = dict(fields)
            fields.pop("progress")
        job.update(fields)
        job["updated"] = time.time()
        _bump()


def _kind_for(path: str) -> str:
    name = path.lower()
    if name.endswith(".info.json"):
        return "json"
    ext = name.rsplit(".", 1)[-1] if "." in os.path.basename(name) else ""
    for kind, exts in _EXT_KIND.items():
        if ext in exts:
            return kind
    return "other"


def add_file(jid: str, path: str, kind: str = "", label: str = "") -> None:
    """Attach a produced file. ``kind`` is inferred from the extension when it
    is empty or not one of FILE_KINDS (older callers pass free-form labels)."""
    if kind not in FILE_KINDS:
        kind = _kind_for(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    name = os.path.basename(path)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        for f in job["files"]:
            if f["path"] == path:
                f["size"] = size
                break
        else:
            job["files"].append({"path": path, "name": name, "kind": kind, "ext": ext,
                                 "size": size, "label": label})
        job["updated"] = time.time()
        _bump()


def set_steps(jid: str, steps: list[tuple[str, str]]) -> None:
    """Declare a transcript's steps up front, all pending."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        job["steps"] = [{"key": k, "label": lbl, "state": "pending", "note": ""}
                        for k, lbl in steps]
        job["updated"] = time.time()
        _bump()


def step(jid: str, key: str, state: str, note: str | None = None,
         label: str | None = None) -> None:
    """Move one step to a new state. Unknown keys are appended, so an engine
    can add a step it only discovers late (a first-time model download)."""
    if state not in STEP_STATES:
        state = "active"
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        for s in job["steps"]:
            if s["key"] == key:
                break
        else:
            s = {"key": key, "label": label or key, "state": "pending", "note": ""}
            job["steps"].append(s)
        s["state"] = state
        if note is not None:
            s["note"] = note
        if label is not None:
            s["label"] = label
        job["updated"] = time.time()
        _bump()


def _slim_result(result):
    if isinstance(result, dict) and ("segments" in result or "text" in result):
        return {k: v for k, v in result.items() if k not in ("segments", "text")}
    return result


def _snapshot(job: dict, slim: bool) -> dict:
    """A copy the caller may serialise outside the lock."""
    out = dict(job)
    out["files"] = [dict(f) for f in job["files"]]
    out["steps"] = [dict(s) for s in job["steps"]]
    out["options"] = dict(job["options"])
    if slim:
        out["result"] = _slim_result(job["result"])
    return out


def get(jid: str, slim: bool = False) -> dict | None:
    with _lock:
        job = _jobs.get(jid)
        return _snapshot(job, slim) if job else None


def all_jobs(slim: bool = True) -> list[dict]:
    """Every job, newest first. Slim by default: the SSE stream sends this on
    every tick, and full transcripts made each tick tens of kilobytes."""
    with _lock:
        return [_snapshot(j, slim) for j in sorted(_jobs.values(), key=lambda j: -j["created"])]


def full_result(jid: str) -> dict | None:
    """The worker's full return value, segments and text included."""
    with _lock:
        job = _jobs.get(jid)
        if not job or not isinstance(job.get("result"), dict):
            return None
        return dict(job["result"])


def cancel(jid: str) -> bool:
    """Ask a job to stop. A queued job is cancelled on the spot; a running one
    becomes "stopping" until its worker returns. A live recording is never
    "cancelled": stopping it saves what was recorded."""
    with _lock:
        ev = _cancels.get(jid)
        job = _jobs.get(jid)
        if not ev or not job or job["status"] not in ("queued", "running"):
            return False
        ev.set()
        cleared = {"speed": "", "eta": "", "speed_bps": None, "eta_s": None}
        if job["status"] == "queued":
            job.update(status="cancelled", stage="Cancelled", **cleared)
        elif job["kind"] == "live":
            if job.get("live_phase") in ("recording", "saving"):
                job.update(status="stopping", live_phase="saving", indeterminate=True,
                           stage="Saving the recording", **cleared)
            else:
                # Still waiting, or still checking the stream: nothing has
                # been recorded, so there is nothing to save.
                job.update(status="stopping", stage="Stopping", **cleared)
        else:
            job.update(status="stopping", stage="Cancelling", **cleared)
        job["updated"] = time.time()
        _bump()
        return True


def clear_cancel(jid: str) -> None:
    """Kept for engines that still call it: a stopped recording lands on
    "done" whatever the flag says."""
    ev = _cancels.get(jid)
    if ev:
        ev.clear()


def cancelled(jid: str) -> bool:
    """True once the user cancelled. Also True for an id the registry no
    longer knows, so an orphaned worker always stops. No id (a helper called
    outside any job) is never cancelled."""
    if not jid:
        return False
    ev = _cancels.get(jid)
    return True if ev is None else ev.is_set()


def raise_if_cancelled(jid: str) -> None:
    if cancelled(jid):
        raise Cancelled()


def active_count() -> int:
    """Jobs still doing real work. The launcher refuses to quit while any
    exist, so closing the window can never abandon a download, a recording or
    a recording that is still being saved."""
    with _lock:
        return sum(1 for jid, j in _jobs.items()
                   if j["status"] in ACTIVE or (jid in _running and j["status"] != "cancelled"))


def cancel_all() -> int:
    """Stop everything (the launcher is being shut down on purpose)."""
    with _lock:
        ids = [jid for jid, j in _jobs.items() if j["status"] in ("queued", "running")]
    return sum(1 for jid in ids if cancel(jid))


# ------------------------------------------------------------------ running

def submit(jid: str, fn: Callable[..., Any], *args, pool: str = "work", **kwargs) -> None:
    """Run fn(jid, ...) and translate how it ends into the job's final state.

    pool="work" shares the three worker slots; pool="live" gets its own daemon
    thread and never takes a slot.
    """
    with _lock:
        _running.add(jid)
        _recipes[jid] = (fn, args, kwargs, pool)

    def runner():
        final = None
        try:
            with _lock:
                _started.add(jid)
                job = _jobs.get(jid)
                ev = _cancels.get(jid)
                go = bool(job and ev and not ev.is_set())
                if go:
                    job.update(status="running", stage="Starting", message="", error=None)
                    job["updated"] = time.time()
                    _bump()
                kind = job["kind"] if job else ""
                url = job["url"] if job else ""
            if not go:
                final = _finish(jid, status="cancelled", stage="Cancelled")
                return
            try:
                result = fn(jid, *args, **kwargs)
            except Skipped as exc:
                final = _finish(jid, status="skipped", stage=str(exc.reason or "Skipped"))
            except Exception as exc:        # noqa: BLE001 - every failure becomes a card
                # Once the user has cancelled, whatever the worker trips over
                # on its way out (ffmpeg killed mid-write, a half-closed
                # socket) is part of stopping, not a failure to report.
                if isinstance(exc, Cancelled) or cancelled(jid):
                    final = _cancelled_outcome(jid, kind)
                else:
                    err = errors.classify(exc, url, kind)
                    final = _finish(jid, status="error", stage="Failed", error=err,
                                    message=errors.message(err))
                    _adopt_params(jid, err)
                    if isinstance(exc, errors.AppError):
                        # An expected outcome the engine named (not live yet,
                        # a playlist link): one line, not a stack trace.
                        print(f"{kind} {jid}: {err['code']}: {err.get('detail') or ''}"[:500])
                    else:
                        traceback.print_exc()
            else:
                if kind == "live":
                    final = _finish(jid, status="done", stage="Done", progress=1.0,
                                    result=result)
                elif cancelled(jid):
                    final = _finish(jid, status="cancelled", stage="Cancelled", result=result)
                else:
                    final = _finish(jid, status="done", stage="Done", progress=1.0,
                                    result=result)
        except Exception:                   # noqa: BLE001 - bookkeeping must not kill the pool
            traceback.print_exc()
        finally:
            with _lock:
                _running.discard(jid)
                _started.discard(jid)
                _bump()
            if final == "done":
                _drop_upload(get(jid))
            if final:
                save_history()

    if pool == "live":
        threading.Thread(target=runner, name=f"live-{jid}", daemon=True).start()
    else:
        POOL.submit(runner)


def _finish(jid: str, **fields) -> str | None:
    fields.setdefault("speed", "")
    fields.setdefault("eta", "")
    fields.setdefault("speed_bps", None)
    fields.setdefault("eta_s", None)
    fields.setdefault("indeterminate", False)
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return None
        if fields.get("status") in ("done", "skipped", "cancelled"):
            fields.setdefault("error", None)
            fields.setdefault("message", "")
        if job["kind"] == "live":
            fields.setdefault("live_phase", None)
            # A finished job has no next check; give_up_at stays, so a card
            # can still say when waiting gave up.
            fields.setdefault("next_check_at", None)
        job.update(fields)
        job["updated"] = time.time()
        _bump()
        return job["status"]


def _cancelled_outcome(jid: str, kind: str) -> str | None:
    """A stopped recording with something on disk is a finished recording."""
    with _lock:
        job = _jobs.get(jid)
        has_files = bool(job and job["files"])
    if kind == "live" and has_files:
        return _finish(jid, status="done", stage="Done", progress=1.0)
    return _finish(jid, status="cancelled", stage="Cancelled")


def _adopt_params(jid: str, err: dict) -> None:
    """An error that knows the title or thumbnail (a link that is not live)
    fills the card in, so it never shows only the URL."""
    params = err.get("params") or {}
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        if params.get("title") and job["title"] in ("", job["url"]):
            job["title"] = params["title"]
        for key in ("thumbnail", "uploader"):
            if params.get(key) and not job.get(key):
                job[key] = params[key]
        _bump()


# ------------------------------------------------------ retry / remove / clear

def _settled(jid: str, job: dict) -> bool:
    """Finished and safe to drop: its worker has returned, or it was
    cancelled before its worker ever started (that worker, when it does run,
    finds the id unknown and stops at once)."""
    if job["status"] not in FINAL:
        return False
    return jid not in _running or (job["status"] == "cancelled" and jid not in _started)


def retry(jid: str, options: dict | None = None) -> dict | None:
    """Run a finished job again in place: same id, url and options (plus any
    ``options`` patch, e.g. vad off). Returns the reset job, or None when the
    job is unknown, still active, or nothing knows how to run its kind."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return None
        opts = dict(job["options"])
        if options:
            opts.update(options)
        # Cancelled while still waiting for a slot: just take the cancel back.
        if job["status"] == "cancelled" and jid in _running and jid not in _started:
            _cancels.setdefault(jid, Event()).clear()
            job.update(status="queued", stage="Waiting", options=opts)
            job["updated"] = time.time()
            _bump()
            recipe = _recipes.get(jid)
            if options and recipe and len(recipe[1]) >= 2 and isinstance(recipe[1][1], dict):
                recipe[1][1].update(options)
            return _snapshot(job, slim=True)
        if job["status"] not in FINAL or jid in _running:
            return None
        recipe = _recipes.get(jid)
        if recipe:
            fn, args, kwargs, pool = recipe
            if len(args) >= 2 and isinstance(args[1], dict):
                args = (args[0], opts) + tuple(args[2:])
        elif job["kind"] in _runners:
            fn, pool = _runners[job["kind"]]
            args, kwargs = (job["url"], opts), {}
        else:
            return None
        fresh = _new_job(jid, job["kind"], job["url"], opts, job["title"],
                         {"thumbnail": job["thumbnail"], "uploader": job["uploader"]})
        fresh["created"] = job["created"]
        _jobs[jid] = fresh
        _cancels[jid] = Event()
        _bump()
    submit(jid, fn, *args, pool=pool, **kwargs)
    save_history()
    return get(jid, slim=True)


def remove(jid: str) -> bool:
    """Forget one finished job (the × on its card)."""
    with _lock:
        job = _jobs.get(jid)
        if not job or not _settled(jid, job):
            return False
        _forget(jid)
        _bump()
    _drop_upload(job)
    save_history()
    return True


def _forget(jid: str) -> None:
    _jobs.pop(jid, None)
    _cancels.pop(jid, None)
    _recipes.pop(jid, None)


def clear_completed() -> int:
    """Clear done, skipped and cancelled jobs; failures stay so their error
    text is not lost. Cleared jobs can be restored for a few seconds."""
    with _lock:
        gone = [(j, _recipes.get(jid)) for jid, j in _jobs.items()
                if j["status"] in CLEARABLE and _settled(jid, j)]
        for j, _ in gone:
            _forget(j["id"])
        if gone:
            _trash.append((time.time(), gone))
        _bump()
    if gone:
        t = threading.Timer(RESTORE_SECONDS + 0.5, _expire_trash)
        t.daemon = True
        t.start()
        save_history()
    return len(gone)


clear_finished = clear_completed          # the 1.1 name


def restore() -> int:
    """Undo the most recent clear, within RESTORE_SECONDS."""
    now = time.time()
    with _lock:
        if not _trash:
            return 0
        when, items = _trash[-1]
        if now - when > RESTORE_SECONDS:
            return 0
        _trash.pop()
        count = 0
        for job, recipe in items:
            jid = job["id"]
            if jid in _jobs:
                continue
            _jobs[jid] = job
            ev = _cancels[jid] = Event()
            if job["status"] == "cancelled":
                ev.set()          # a worker that has not started yet must still skip it
            if recipe:
                _recipes[jid] = recipe
            count += 1
        _bump()
    save_history()
    return count


def _expire_trash() -> None:
    now = time.time()
    with _lock:
        expired = [t for t in _trash if now - t[0] > RESTORE_SECONDS]
        _trash[:] = [t for t in _trash if now - t[0] <= RESTORE_SECONDS]
    for _, items in expired:
        for job, _ in items:
            _drop_upload(job)


def _drop_upload(job: dict | None) -> None:
    """Delete a local-file transcript's temporary copy once it is no longer
    needed (the job finished, or was removed). Only ever inside UPLOAD_ROOT."""
    if not job:
        return
    local = (job.get("options") or {}).get("local_path")
    if not local:
        return
    # Judged lexically: the server built this path itself, and resolve() on
    # Windows can hand back a \\?\ form that never matches the root.
    path = Path(os.path.normcase(os.path.abspath(local)))
    root = Path(os.path.normcase(os.path.abspath(UPLOAD_ROOT)))
    if root not in path.parents:
        return
    folder = path.parent if path.parent != root else None
    try:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------ history

def history_path() -> Path:
    return config.DATA_ROOT / "history.json"


def _history_record(job: dict) -> dict:
    rec = _snapshot(job, slim=True)
    rec.pop("from_history", None)
    return rec


def save_history() -> None:
    """Persist finished jobs (newest 200), without transcript text, so the
    Queue and "View transcript" survive a restart.

    The snapshot is taken inside the file lock: two workers finishing at once
    must write in the order they looked, or the older view lands last and the
    newest finished job is missing from the file."""
    path = history_path()
    with _hist_lock:
        with _lock:
            done = [j for j in _jobs.values()
                    if j["status"] in FINAL and j["id"] not in _running]
            done.sort(key=lambda j: j["updated"], reverse=True)
            records = [_history_record(j) for j in done[:HISTORY_LIMIT]]
        # default=str: one odd value in an engine's result must not cost
        # the whole history.
        text = json.dumps({"version": 1, "jobs": records}, ensure_ascii=False, default=str)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            print(f"jobs: could not save history: {exc}")


def load_history() -> int:
    """Bring back the previous sessions' finished jobs, flagged from_history.
    New ids continue after the highest one seen, so they never collide."""
    global _ids
    path = history_path()
    try:
        data = json.loads(path.read_text("utf-8-sig"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        print(f"jobs: history unreadable, starting empty: {exc}")
        return 0
    records = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return 0
    loaded = 0
    top = 0
    with _lock:
        for rec in records[:HISTORY_LIMIT]:
            if not isinstance(rec, dict) or not rec.get("id") or rec.get("status") not in FINAL:
                continue
            jid = str(rec["id"])
            m = re.fullmatch(r"j(\d+)", jid)
            if m:
                top = max(top, int(m.group(1)))
            if jid in _jobs:
                continue
            options = rec.get("options") if isinstance(rec.get("options"), dict) else {}
            job = _new_job(jid, str(rec.get("kind") or "download"), str(rec.get("url") or ""),
                           options, str(rec.get("title") or ""), {})
            # Only values of the right shape: one hand-edited or truncated
            # record must not break every snapshot, and with it /api/jobs.
            for key, value in rec.items():
                if key not in job or key in ("id", "options", "from_history"):
                    continue
                default = job[key]
                if key in ("files", "steps"):
                    need = "path" if key == "files" else "key"
                    if isinstance(value, list):
                        job[key] = [dict(v) for v in value
                                    if isinstance(v, dict) and isinstance(v.get(need), str)]
                elif default is None or isinstance(value, type(default)) or \
                        (isinstance(default, float) and isinstance(value, int)):
                    job[key] = value
            job["from_history"] = True
            _jobs[jid] = job
            _cancels[jid] = Event()
            loaded += 1
        current = next(_ids)
        _ids = itertools.count(max(current, top + 1))
        _bump()
    return loaded
