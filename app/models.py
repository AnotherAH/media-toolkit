"""Whisper model download: real files, verified, and self-healing.

Two problems had to be designed out.

**Symlinks.** Hugging Face's default cache stores one copy under `blobs/` and
points at it from `snapshots/` with a relative symlink. On Windows those links
are not reliably followable: measured on this machine, the blob was present and
complete at 145 MB, the link pointed at the right name, and Windows still
answered "cannot find the path specified" when opening it. `os.path.realpath`
hides the problem because it falls back to joining the strings, so the path
*looks* fine while every real open fails. So we download into a plain directory
of real files instead: no blobs, no links, and half the disk usage.

**Silent truncation.** faster-whisper downloads models with the progress bar
hardcoded off and then trusts whatever is in the cache. A multi-gigabyte
download with no feedback looks like a hang, people close the app, and the next
run fails forever with "Unable to open file 'model.bin'" because the directory
exists. So we report progress, verify the result, and repair automatically.

Model names arrive from HTTP requests, so a name only ever becomes a path after
it has been matched against the fixed catalog below, and every path is checked
to sit inside the models folder before anything is deleted.
"""
from __future__ import annotations

import errno
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

# Anything smaller than this is not a real Whisper model. It is the remains of
# an interrupted download.
MIN_MODEL_BYTES = 5 * 1024 * 1024
REQUIRED = ("model.bin", "config.json", "tokenizer.json")
OPTIONAL = ("preprocessor_config.json", "vocabulary.json", "vocabulary.txt")

# Every name faster-whisper understands, and the Hugging Face repo behind it.
# Kept here rather than read from faster_whisper.utils so that listing models
# never loads CTranslate2 and its native runtimes; a test keeps the two in sync.
REPOS: dict[str, str] = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "tiny": "Systran/faster-whisper-tiny",
    "base.en": "Systran/faster-whisper-base.en",
    "base": "Systran/faster-whisper-base",
    "small.en": "Systran/faster-whisper-small.en",
    "small": "Systran/faster-whisper-small",
    "medium.en": "Systran/faster-whisper-medium.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# The models the app offers, best default first. size_mb is the download size,
# used for "1.6 GB" labels before anything is on disk. translates=False marks
# models that ignore task=translate: the turbo model was not trained for it and
# the distil and .en models only ever write English.
CATALOG: list[dict] = [
    {"id": "large-v3-turbo", "label": "Large v3 Turbo", "note": "best balance",
     "size_mb": 1550, "size_label": "1.6 GB", "translates": False},
    {"id": "large-v3", "label": "Large v3", "note": "most accurate, slower",
     "size_mb": 3100, "size_label": "3.1 GB", "translates": True},
    {"id": "distil-large-v3", "label": "Distil Large v3", "note": "English only, fast",
     "size_mb": 1510, "size_label": "1.5 GB", "translates": False},
    {"id": "medium", "label": "Medium", "note": "", "size_mb": 1530, "size_label": "1.5 GB",
     "translates": True},
    {"id": "small", "label": "Small", "note": "good on a processor", "size_mb": 484,
     "size_label": "480 MB", "translates": True},
    {"id": "base", "label": "Base", "note": "", "size_mb": 145, "size_label": "145 MB",
     "translates": True},
    {"id": "tiny", "label": "Tiny", "note": "", "size_mb": 75, "size_label": "75 MB",
     "translates": True},
]
for _m in CATALOG:
    # The Speech model option text, e.g. "Large v3 Turbo · best balance · 1.6 GB".
    _m["option_label"] = " · ".join(p for p in (_m["label"], _m["note"], _m["size_label"]) if p)
_BY_ID = {m["id"]: m for m in CATALOG}


def _hf_environment() -> None:
    """Privacy and tidiness defaults for huggingface_hub, set before it loads.

    Without these it sends any Hugging Face token another tool cached on this
    PC, reports an agent user-agent, and writes caches into the user profile
    that uninstalling Media Toolkit would leave behind. Xet is off because its
    native transfer ignores our progress meter, so a cancelled download could
    not be stopped, and it keeps a chunk cache outside the app's folder.
    """
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HOME", str(config.DATA_ROOT / "hf"))


_hf_environment()


# ------------------------------------------------------------------ catalog

def known(name: str) -> bool:
    return isinstance(name, str) and name in REPOS


def validate(name: str) -> str:
    """Return name if it is a model we know; anything else is refused before
    it can reach the filesystem."""
    if not known(name):
        raise ValueError(f"Unknown Whisper model: {str(name)[:60]!r}")
    return name


def canonical(name: str) -> str:
    """The catalog id for an alias ('turbo' -> 'large-v3-turbo')."""
    validate(name)
    if name in _BY_ID:
        return name
    repo = REPOS[name]
    return next((m["id"] for m in CATALOG if REPOS[m["id"]] == repo), name)


def label(name: str) -> str:
    """Display name for any known model id or alias."""
    cid = canonical(name)
    if cid in _BY_ID:
        return _BY_ID[cid]["label"]
    english = cid.endswith(".en")
    words = cid.removesuffix(".en").split("-")
    text = " ".join(w if w[:1] == "v" and w[1:2].isdigit() else w.capitalize() for w in words)
    return text + (" (English only)" if english else "")


def can_translate(name: str) -> bool:
    cid = canonical(name)
    if cid in _BY_ID:
        return _BY_ID[cid]["translates"]
    return not (cid.endswith(".en") or cid.startswith("distil-") or "turbo" in cid)


def catalog() -> list[dict]:
    return [dict(m) for m in CATALOG]


def cache_root() -> Path:
    root = config.DATA_ROOT / "models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def repo_id(name: str) -> str:
    return REPOS[validate(name)]


def _inside_root(path: Path) -> Path:
    root = cache_root().resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Refusing a model path outside {root}")
    return path


def model_dir(name: str) -> Path:
    """Plain directory of real files, one per model repo."""
    return _inside_root(cache_root() / repo_id(name).replace("/", "--"))


def _legacy_dir(name: str) -> Path:
    return _inside_root(cache_root() / ("models--" + repo_id(name).replace("/", "--")))


def wanted(filename: str) -> bool:
    return filename in REQUIRED or filename in OPTIONAL or filename.startswith("vocabulary.")


def verify(path: Path | None) -> tuple[bool, str]:
    """Is this directory actually loadable? Opens the weights rather than
    trusting stat(), because a broken Windows symlink passes a size check but
    fails the open that CTranslate2 will do later."""
    if not path or not path.is_dir():
        return False, "not downloaded yet"
    for fname in REQUIRED:
        f = path / fname
        try:
            with f.open("rb") as fh:
                head = fh.read(4)
            size = f.stat().st_size
        except OSError as exc:
            return False, f"{fname} cannot be opened ({getattr(exc, 'strerror', exc)})"
        if not head:
            return False, f"{fname} is empty"
        if fname == "model.bin" and size < MIN_MODEL_BYTES:
            return False, f"model.bin is only {size:,} bytes, so the download was cut short"
    return True, ""


def _legacy_snapshot(name: str) -> Path | None:
    """A previously downloaded model in Hugging Face's own cache layout.

    Reused when it still verifies, so upgrading does not re-download gigabytes.
    """
    snaps = _legacy_dir(name) / "snapshots"
    if not snaps.is_dir():
        return None
    for snap in sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if snap.is_dir() and verify(snap)[0]:
            return snap
    return None


def local_path(name: str) -> str | None:
    """Where a verified copy already is, or None. Never touches the network."""
    target = model_dir(name)
    if verify(target)[0]:
        return str(target)
    legacy = _legacy_snapshot(name)
    return str(legacy) if legacy else None


class ModelBusy(RuntimeError):
    """The model is downloading (here or in another copy of the app), so its
    folder cannot be deleted now. The API answers it with 409 Conflict."""


def purge(name: str) -> None:
    """Delete a model so it downloads afresh. Refused while it is downloading."""
    validate(name)
    with _registry_lock:
        busy = any(REPOS[n] == REPOS[name] for n in _downloads)
    if busy:
        raise ModelBusy("That speech model is downloading right now.")
    target = model_dir(name)
    legacy = _legacy_dir(name)
    try:
        with _file_lock(_lock_path(target), None):
            shutil.rmtree(target, ignore_errors=True)
            shutil.rmtree(legacy, ignore_errors=True)
    except _Aborted:
        raise ModelBusy("That speech model is downloading right now.") from None
    if target.exists() or legacy.exists():
        # Windows keeps a file that another program has open; saying "deleted"
        # while gigabytes stay on disk would be untrue.
        raise OSError(errno.EACCES, "Some of its files are in use by another program. "
                      "Close it and try again.")


def expected_bytes(name: str) -> int:
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo_id(name), files_metadata=True)
    except Exception:
        return 0
    return sum(s.size or 0 for s in (info.siblings or []) if wanted(s.rfilename))


def _dir_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


# ---------------------------------------------------------------- downloads
#
# One download per model, shared by everyone who needs it. A transcript job
# waits on it and can walk away when the user cancels; the download itself is
# stopped only when nobody is waiting any more (and it was not started from
# Settings), so a second job that needs the same model is never cut off.

class _Aborted(Exception):
    """Raised inside the download thread once nobody wants the model any more."""


class _Download:
    def __init__(self, name: str, keep: bool):
        self.name = name
        self.keep = keep                    # started from Settings: finish regardless
        self.waiters = 0
        self.abort = threading.Event()
        self.done = threading.Event()
        self.bytes_done = 0
        self.bytes_total = 0
        self.started = False                # real bytes are being fetched
        self.error: BaseException | None = None
        self.path = ""
        self.listeners: list = []
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        return {"name": self.name, "label": label(self.name), "busy": not self.done.is_set(),
                "downloading": self.started, "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total,
                "error": str(self.error)[:300] if self.error else ""}


_registry_lock = threading.Lock()
_downloads: dict[str, _Download] = {}
_last: dict[str, dict] = {}            # finished background downloads, for Settings
_proxy_seen: str | None = None
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
               "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


def _sync_proxy() -> None:
    """Model downloads follow the proxy set in Settings.

    config.apply_proxy_env() exports that proxy to the environment, which the
    HTTP client huggingface_hub uses reads when it is created. That client is
    created once and then reused, so when the proxy has changed since, it is
    closed and the next request builds a fresh one with the new setting.
    """
    global _proxy_seen
    current = "|".join(os.environ.get(k, "") for k in _PROXY_VARS)
    if current == _proxy_seen:
        return
    try:
        from huggingface_hub import close_session
        close_session()
    except Exception:
        pass
    _proxy_seen = current


def _quiet_meter(abort: threading.Event):
    """A progress-bar class for huggingface_hub that shows nothing and stops the
    transfer at the next chunk once abort is set."""
    from tqdm import tqdm

    class Meter(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if abort.is_set():
                raise _Aborted()
            return super().update(n)

    return Meter


def _lock_path(target: Path) -> Path:
    return cache_root() / f".{target.name}.lock"


@contextmanager
def _file_lock(path: Path, abort: threading.Event | None):
    """Hold an exclusive lock that other Media Toolkit processes also respect,
    so a second copy of the app never deletes a download in progress.

    Waits until abort is set; with abort=None it tries once and raises
    _Aborted when another process holds the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if abort is None or abort.wait(0.5):
                    raise _Aborted()
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()


def _notify(dl: _Download) -> None:
    for fn in list(dl.listeners):
        try:
            fn(dl.bytes_done, dl.bytes_total)
        except Exception:
            pass


def _watch(dl: _Download, path: Path, stop: threading.Event) -> None:
    """Report progress by watching bytes land on disk, independent of which
    transport Hugging Face picks."""
    while not stop.wait(1.0):
        size = _dir_size(path)
        dl.bytes_done = min(size, dl.bytes_total) if dl.bytes_total else size
        _notify(dl)


def _run(dl: _Download) -> None:
    name = dl.name
    target = model_dir(name)
    try:
        with _file_lock(_lock_path(target), dl.abort):
            if verify(target)[0]:             # another process just finished it
                dl.path = str(target)
                return
            if (legacy := _legacy_snapshot(name)):
                dl.path = str(legacy)
                return
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True, exist_ok=True)
            try:
                _sync_proxy()
                dl.bytes_total = expected_bytes(name)
                dl.started = True
                _notify(dl)
                stop = threading.Event()
                watcher = threading.Thread(target=_watch, args=(dl, target, stop),
                                           daemon=True, name=f"model-watch-{name}")
                watcher.start()
                try:
                    _download_files(name, target, dl.abort)
                finally:
                    stop.set()
                    watcher.join(timeout=3)
                ok, why = verify(target)
                if not ok:
                    raise RuntimeError(
                        f"The {name} model did not download correctly ({why}). "
                        "Check your connection and try again.")
            except BaseException:
                # Still holding the lock: no other process can be writing here.
                # A half model is worse than none, since it looks installed.
                shutil.rmtree(target, ignore_errors=True)
                raise
            dl.bytes_done = dl.bytes_total or _dir_size(target)
            _notify(dl)
            dl.path = str(target)
    except BaseException as exc:            # noqa: BLE001  handed to the waiters
        dl.error = exc
    finally:
        with _registry_lock:
            if _downloads.get(name) is dl:
                _downloads.pop(name, None)
            if dl.keep:
                # Recorded before done is set, so say finished here: Settings
                # would otherwise show this download as running forever.
                _last[name] = {**dl.snapshot(), "busy": False}
        dl.done.set()


def _start(name: str, keep: bool) -> _Download:
    """Join the running download for this model, or start one. Caller holds
    _registry_lock."""
    dl = _downloads.get(name)
    if dl is not None and not dl.abort.is_set():
        dl.keep = dl.keep or keep
        return dl
    if dl is not None:
        # An abandoned download is still winding down; wait for its thread
        # outside the lock, then start fresh.
        return dl
    dl = _Download(name, keep)
    _downloads[name] = dl
    dl.thread = threading.Thread(target=_run, args=(dl,), daemon=True, name=f"model-{name}")
    dl.thread.start()
    return dl


def ensure(name: str, on_progress=None, check=None, on_status=None) -> str:
    """Return a directory holding verified model files, downloading if needed.

    on_progress(done, total) fires once when a real download starts and then
    about once a second. check() is called twice a second while waiting and may
    raise (a cancelled job); the wait then ends at once, and the download stops
    too unless someone else still needs the model. on_status(text) gets a few
    plain milestones, for the command-line diagnosis.
    """
    name = canonical(name)
    if (ready := local_path(name)):
        if on_status:
            on_status(f"{label(name)} is already downloaded")
        return ready
    if on_status:
        announced = []

        def report(done: int, total: int, _inner=on_progress) -> None:
            if not announced:
                announced.append(True)
                size = f" ({total / 1048576:,.0f} MB)" if total else ""
                on_status(f"Downloading {label(name)}{size}")
            if _inner:
                _inner(done, total)

        on_progress = report

    while True:
        with _registry_lock:
            dl = _start(name, keep=False)
            if dl.abort.is_set():
                stale = dl
            else:
                stale = None
                dl.waiters += 1
                if on_progress:
                    dl.listeners.append(on_progress)
        if stale is None:
            break
        stale.done.wait()

    try:
        if on_progress and dl.started:
            on_progress(dl.bytes_done, dl.bytes_total)
        while not dl.done.wait(0.5):
            if check:
                check()
    finally:
        with _registry_lock:
            dl.waiters -= 1
            if on_progress in dl.listeners:
                dl.listeners.remove(on_progress)
            if dl.waiters <= 0 and not dl.keep and not dl.done.is_set():
                dl.abort.set()

    if dl.error is not None:
        if isinstance(dl.error, _Aborted):
            raise RuntimeError("The speech model download was stopped.")
        raise dl.error
    if on_status:
        on_status(f"{label(name)} is ready")
    return dl.path


def start_download(name: str) -> dict:
    """Download (or repair) a model in the background, for Settings'
    'Download again now'. Returns the current state; poll download_state()."""
    name = canonical(name)
    if local_path(name):
        return {"name": name, "label": label(name), "busy": False, "downloading": False,
                "bytes_done": 0, "bytes_total": 0, "error": ""}
    for _ in range(40):
        with _registry_lock:
            dl = _start(name, keep=True)
            if not dl.abort.is_set():
                _last.pop(name, None)
                return dl.snapshot()
        # A cancelled transcript's download of this model is still stopping
        # (it quits at its next chunk); joining it would end with nothing.
        dl.done.wait(0.25)
    return dl.snapshot()


def download_state() -> list[dict]:
    """Downloads running now, plus the outcome of recent background ones."""
    with _registry_lock:
        running = [dl.snapshot() for dl in _downloads.values()]
        names = {d["name"] for d in running}
        return running + [v for k, v in _last.items() if k not in names]


def _download_files(name: str, dest: Path, abort: threading.Event | None = None) -> None:
    """Fetch each file as a real file into dest.

    `local_dir` keeps Hugging Face from using its blob-and-symlink cache, and
    downloading one file at a time turns a failure into an actual error instead
    of a silently incomplete directory.
    """
    from huggingface_hub import HfApi, hf_hub_download

    abort = abort or threading.Event()
    meter = _quiet_meter(abort)
    repo = repo_id(name)
    try:
        listing = [s.rfilename for s in (HfApi().model_info(repo).siblings or [])]
    except Exception:
        listing = []
    # Without the listing, ask for the optional files too: the large-v3 family
    # needs preprocessor_config.json (128 mel bands, not the default 80), and
    # a model missing it would load and then fail every run. A file the repo
    # does not have is just skipped below.
    targets = [f for f in listing if wanted(f)] or list(REQUIRED) + list(OPTIONAL)
    # Weights last, so an interrupted run never leaves a complete-looking folder.
    targets.sort(key=lambda f: f == "model.bin")

    for fname in targets:
        if abort.is_set():
            raise _Aborted()
        try:
            hf_hub_download(repo, fname, local_dir=str(dest), tqdm_class=meter)
        except _Aborted:
            raise
        except Exception as exc:
            # A file the repo lists is part of the model, so failing to fetch
            # it fails the download; only guessed names may be missing.
            if fname in REQUIRED or listing:
                raise RuntimeError(f"could not fetch {fname}: {str(exc)[:200]}") from exc
            continue                          # optional extras vary between repos

    # hf_hub leaves its bookkeeping behind; the loader only wants the weights.
    shutil.rmtree(dest / ".cache", ignore_errors=True)


def installed() -> list[dict]:
    """Which models are on disk, and are they intact?

    One row per model folder, named by its catalog id and display label; the
    aliases that share the same files are listed but never shown as rows.
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    order = [m["id"] for m in CATALOG] + [n for n in REPOS if n not in _BY_ID]
    with _registry_lock:
        busy = {REPOS[n] for n in _downloads}
    for name in order:
        new_dir = model_dir(name)
        legacy = _legacy_dir(name)
        path = new_dir if new_dir.is_dir() else (legacy if legacy.is_dir() else None)
        if not path:
            continue
        key = path.name
        if key in seen:
            seen[key]["aliases"].append(name)
            continue
        downloading = REPOS[name] in busy
        checked = new_dir if new_dir.is_dir() else _legacy_snapshot(name)
        ok, why = verify(checked) if checked else (False, "no files")
        entry = {"name": name, "label": label(name), "aliases": [],
                 "ok": ok and not downloading,
                 "downloading": downloading,
                 "problem": "" if ok else ("downloading" if downloading else why),
                 "size_mb": round(_dir_size(path) / 1048576),
                 "download_mb": _BY_ID.get(name, {}).get("size_mb", 0),
                 "size_label": _BY_ID.get(name, {}).get("size_label", ""),
                 "layout": "files" if new_dir.is_dir() else "legacy cache"}
        seen[key] = entry
        out.append(entry)
    return out
