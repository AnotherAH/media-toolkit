"""Whisper model download: real files, verified, and self-healing.

Two problems had to be designed out.

**Symlinks.** Hugging Face's default cache stores one copy under `blobs/` and
points at it from `snapshots/` with a relative symlink. On Windows those links
are not reliably followable: measured on this machine, the blob was present and
complete at 145 MB, the link pointed at the right name, and Windows still
answered "cannot find the path specified" when opening it. `os.path.realpath`
hides the problem because it falls back to joining the strings, so the path
*looks* fine while every real open fails. So we download into a plain directory
of real files instead -- no blobs, no links, and half the disk usage.

**Silent truncation.** faster-whisper downloads models with the progress bar
hardcoded off and then trusts whatever is in the cache. A multi-gigabyte
download with no feedback looks like a hang, people close the app, and the next
run fails forever with "Unable to open file 'model.bin'" because the directory
exists. So we report progress, verify the result, and repair automatically.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from . import config

# Anything smaller than this is not a real Whisper model -- it is the remains of
# an interrupted download.
MIN_MODEL_BYTES = 5 * 1024 * 1024
REQUIRED = ("model.bin", "config.json", "tokenizer.json")
OPTIONAL = ("preprocessor_config.json", "vocabulary.json", "vocabulary.txt")


def cache_root() -> Path:
    root = config.DATA_ROOT / "models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def repo_id(name: str) -> str:
    from faster_whisper.utils import _MODELS
    if "/" in name:
        return name
    repo = _MODELS.get(name)
    if not repo:
        raise ValueError(f"Unknown Whisper model: {name}")
    return repo


def model_dir(name: str) -> Path:
    """Plain directory of real files, one per model repo."""
    return cache_root() / repo_id(name).replace("/", "--")


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
            return False, f"model.bin is only {size:,} bytes -- download was cut short"
    return True, ""


def _legacy_snapshot(name: str) -> Path | None:
    """A previously downloaded model in Hugging Face's own cache layout.

    Reused when it still verifies, so upgrading does not re-download gigabytes.
    """
    base = cache_root() / ("models--" + repo_id(name).replace("/", "--"))
    snaps = base / "snapshots"
    if not snaps.is_dir():
        return None
    for snap in sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if snap.is_dir() and verify(snap)[0]:
            return snap
    return None


def purge(name: str) -> None:
    shutil.rmtree(model_dir(name), ignore_errors=True)
    shutil.rmtree(cache_root() / ("models--" + repo_id(name).replace("/", "--")),
                  ignore_errors=True)


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


def _watch(path: Path, total: int, on_progress, stop) -> None:
    """Report progress by watching bytes land on disk -- independent of which
    transport Hugging Face picks (Xet ignores the progress-bar hook entirely)."""
    while not stop.wait(1.0):
        if total:
            on_progress(min(_dir_size(path), total), total)


def ensure(name: str, on_status=None, on_progress=None) -> str:
    """Return a directory holding verified model files, downloading if needed."""
    def status(text: str):
        if on_status:
            on_status(text)

    target = model_dir(name)
    ok, why = verify(target)
    if ok:
        return str(target)

    if (legacy := _legacy_snapshot(name)):
        return str(legacy)                    # already downloaded, still good

    if target.exists():
        status(f"Repairing {name} ({why})")
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    total = expected_bytes(name)
    status(f"Downloading {name}"
           + (f" ({total / 1048576:.0f} MB, one time)" if total else " (one time)"))

    stop = threading.Event()
    watcher = None
    if on_progress and total:
        watcher = threading.Thread(target=_watch,
                                   args=(target, total, on_progress, stop), daemon=True)
        watcher.start()
    try:
        _download_files(name, target)
    finally:
        stop.set()
        if watcher:
            watcher.join(timeout=3)

    ok, why = verify(target)
    if not ok:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"The {name} model did not download correctly ({why}). "
            "Check your connection and try again -- it will be re-fetched.")
    return str(target)


def _download_files(name: str, dest: Path) -> None:
    """Fetch each file as a real file into dest.

    `local_dir` keeps Hugging Face from using its blob-and-symlink cache, and
    downloading one file at a time turns a failure into an actual error instead
    of a silently incomplete directory.
    """
    from huggingface_hub import HfApi, hf_hub_download

    repo = repo_id(name)
    try:
        listing = [s.rfilename for s in (HfApi().model_info(repo).siblings or [])]
    except Exception:
        listing = []
    targets = [f for f in listing if wanted(f)] or list(REQUIRED) + ["vocabulary.json"]
    # Weights last, so an interrupted run never leaves a complete-looking folder.
    targets.sort(key=lambda f: f == "model.bin")

    for fname in targets:
        try:
            hf_hub_download(repo, fname, local_dir=str(dest))
        except Exception as exc:
            if fname in REQUIRED:
                raise RuntimeError(f"could not fetch {fname}: {str(exc)[:160]}") from exc
            continue                          # optional extras vary between repos

    # hf_hub leaves its bookkeeping behind; the loader only wants the weights.
    shutil.rmtree(dest / ".cache", ignore_errors=True)


def installed() -> list[dict]:
    """Which models are on disk, and are they intact?"""
    from faster_whisper.utils import _MODELS
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for name in _MODELS:
        try:
            new_dir = model_dir(name)
        except ValueError:
            continue
        legacy = cache_root() / ("models--" + repo_id(name).replace("/", "--"))
        path = new_dir if new_dir.is_dir() else (legacy if legacy.is_dir() else None)
        if not path:
            continue
        key = path.name
        if key in seen:
            seen[key]["aliases"].append(name)
            continue
        checked = new_dir if new_dir.is_dir() else _legacy_snapshot(name)
        ok, why = verify(checked) if checked else (False, "no files")
        entry = {"name": name, "aliases": [], "ok": ok, "problem": why,
                 "size_mb": round(_dir_size(path) / 1048576),
                 "layout": "files" if new_dir.is_dir() else "legacy cache"}
        seen[key] = entry
        out.append(entry)
    return out
