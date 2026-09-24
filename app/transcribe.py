"""Speech recognition via faster-whisper, plus the transcript job orchestrator.

Backend selection is proven, not assumed: candidate (device, compute_type) pairs
come from hardware.py and each one is loaded and made to run a second of audio
before we trust it. The first pair that survives is remembered, so later runs start
instantly. A machine with no GPU, an old GPU, or a broken CUDA install still
ends up on a working CPU backend rather than an error.

Audio always reaches Whisper as a numpy array decoded by ffmpeg in its own
process (see audio.py), never as a path, so PyAV is never needed.

Only one Whisper model is kept in memory, and only one transcription runs at
a time. Two large models side by side do not fit most graphics cards, and a
second run on the same card only halves the speed of the first.
"""
from __future__ import annotations

import gc
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import audio, captions, config, hardware, jobs, models, subs
from .errors import AppError

# Kept for older callers; the catalog itself lives in models.py.
MODELS = models.CATALOG

STEP_LABELS = {
    "reading": "Reading the link",
    "reading_file": "Reading the file",
    "captions": "Looking for captions",
    "audio": "Getting the audio",
    "model_download": "Downloading the speech model (one time only)",
    "model_load": "Loading the speech model",
    "transcribing": "Transcribing on this PC",
    "transcribing_cuda": "Transcribing on your graphics card",
    "transcribing_cpu": "Transcribing on this PC's processor",
    "saving": "Saving",
}
NOTE_CAPTIONS_FAILED = "couldn't load captions, transcribing instead"
STAGE_WAITING = "Waiting for another transcription to finish"

# Speech needs little: the smallest audio-only stream is plenty for 16 kHz mono.
AUDIO_FORMAT = "ba[abr<=160]/ba/b[height<=480]/w"
TRANSCRIPT_EXTS = ("txt", "srt", "md", "vtt", "json", "mt.json")
DEFAULT_FORMATS = ("txt", "srt", "md")

_state_lock = threading.Lock()      # guards _loaded
_loaded: tuple[tuple, object] | None = None
_run_lock = threading.Lock()        # one Whisper run at a time
_save_lock = threading.Lock()       # choosing a file name and writing it is one step
_ort_quiet = False


# ------------------------------------------------------------------ catalog

def model_catalog() -> list[dict]:
    """The models the Transcript options offer, with what the UI needs to label
    them: display label, download size, whether it fits the graphics memory,
    is downloaded, is recommended, and can translate."""
    hw = hardware.summary()
    vram = hw["vram_mb"] if hw["gpu_ready"] else 0
    have = set()
    for entry in models.installed():
        if entry["ok"]:
            have.add(entry["name"])
            have.update(entry["aliases"])
    out = []
    for m in models.catalog():
        need = hardware.estimated_vram(m["id"], "int8_float16" if vram else "int8")
        out.append({**m,
                    "fits": vram == 0 or need <= vram,
                    "vram_mb": hardware.MODEL_VRAM.get(m["id"], 0),
                    "installed": m["id"] in have,
                    "recommended": m["id"] == hw["recommended_model"],
                    "can_translate": m["translates"]})
    return out


# ------------------------------------------------------------ model loading

def _whisper_class():
    """Import faster-whisper without PyAV if PyAV is missing or broken."""
    audio.install_av_stub()
    try:
        from faster_whisper import WhisperModel
        return WhisperModel
    except ImportError:
        try:
            import av  # noqa: F401
        except Exception:
            # PyAV is there but cannot load: replace it and import again.
            audio.install_av_stub(force=True)
            for key in [k for k in sys.modules if k.startswith("faster_whisper")]:
                sys.modules.pop(key, None)
            from faster_whisper import WhisperModel
            return WhisperModel
        raise


def _quiet_onnxruntime() -> None:
    """The official onnxruntime build (used for silence detection) has its
    telemetry switched on by default. Switch it off before first use."""
    global _ort_quiet
    if _ort_quiet:
        return
    _ort_quiet = True
    try:
        import onnxruntime
        fn = getattr(onnxruntime, "disable_telemetry_events", None)
        if fn:
            fn()
    except Exception:
        pass


def _warmup(model) -> None:
    """Force the encoder to actually run so a broken backend fails here, loudly,
    instead of halfway through a real transcription."""
    samples = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32)
    segments, _ = model.transcribe(samples, beam_size=1, vad_filter=False, language="en")
    for _ in segments:
        break


def unload() -> None:
    """Drop the loaded model and give its memory back (CTranslate2 releases
    graphics memory when the model object is collected)."""
    global _loaded
    with _state_lock:
        _loaded = None
    gc.collect()


# A loaded model holds gigabytes of graphics memory that games and video
# editors want back. Keeping it for a while makes the next transcript start
# at once; after this long without one it is released.
IDLE_UNLOAD_SECONDS = 15 * 60
_idle_timer: threading.Timer | None = None


def _idle_unload() -> None:
    if _run_lock.acquire(blocking=False):      # never under a running transcription
        try:
            unload()
        finally:
            _run_lock.release()


def _schedule_idle_unload() -> None:
    global _idle_timer
    timer = threading.Timer(IDLE_UNLOAD_SECONDS, _idle_unload)
    timer.daemon = True
    with _state_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = timer
    timer.start()


def load_model(name: str, device_pref: str = "auto", compute_pref: str = "auto",
               check=None) -> tuple[object, str, str]:
    """Return (model, device, compute_type), trying candidates until one works.

    The previous model is released before a different one loads, so switching
    models or precisions never stacks copies in graphics memory.
    """
    global _loaded
    WhisperModel = _whisper_class()
    path = models.ensure(name, check=check)
    auto = device_pref in ("auto", "", None) and compute_pref in ("auto", "", None)
    order = hardware.candidates(name, device_pref or "auto", compute_pref or "auto")
    cached = hardware.recall(name) if auto else None
    if cached and tuple(cached) in [tuple(c) for c in order]:
        order = [tuple(cached)] + [c for c in order if tuple(c) != tuple(cached)]

    with _state_lock:
        if _loaded and _loaded[0][0] == path and tuple(_loaded[0][1:]) in [tuple(c) for c in order]:
            return _loaded[1], _loaded[0][1], _loaded[0][2]

    errors: list[str] = []
    oom = False
    cpu_failed = False
    for device, compute in order:
        unload()
        if check:
            check()
        try:
            model = WhisperModel(
                path, device=device, compute_type=compute,
                cpu_threads=min(16, os.cpu_count() or 4) if device == "cpu" else 0,
                num_workers=1,
            )
            _warmup(model)
        except Exception as exc:
            model = None
            gc.collect()
            text = str(exc)
            low = text.lower()
            if "unable to open file" in low:
                # The weights are corrupt, not the backend. Remove them so the
                # next attempt downloads a fresh copy.
                try:
                    models.purge(name)
                except Exception:
                    pass
                raise AppError("model_damaged", text) from exc
            oom = oom or "out of memory" in low
            cpu_failed = cpu_failed or device == "cpu"
            errors.append(f"{device}/{compute}: {text[:200]}")
            continue
        with _state_lock:
            _loaded = ((path, device, compute), model)
        # A processor fallback caused by a full graphics card, or by a CUDA
        # failure earlier this session, is not a verdict on the card:
        # remembering it would keep the GPU off for good, across restarts.
        if auto and not (device == "cpu" and (oom or hardware.cuda_failed())):
            hardware.remember(name, device, compute)
        return model, device, compute

    detail = "No working transcription backend. Tried:\n  " + "\n  ".join(errors)
    code = "unknown" if cpu_failed else ("out_of_memory" if oom else "gpu_failed")
    raise AppError(code, detail)


@contextmanager
def _whisper_slot(check=None, on_wait=None):
    """Hold the single Whisper slot, polling for cancellation while waiting."""
    waited = False
    while not _run_lock.acquire(timeout=0.5):
        if not waited and on_wait:
            on_wait()
        waited = True
        if check:
            check()
    try:
        yield
    finally:
        _run_lock.release()


# --------------------------------------------------------------- options

def _options(o: dict, cfg: dict) -> dict:
    """Normalise the transcript options from the request (old and new UI)."""
    language = str(o.get("language", cfg.get("transcript_language", "")) or "").strip()[:20]
    translate = bool(o.get("translate"))
    if language.lower() in ("auto", "same"):
        language = ""
    if language.lower() == "translate":
        language, translate = "", True
    prefer = o.get("prefer_captions")
    if prefer is None:
        prefer = cfg.get("prefer_native_subs", True)
    prefer = bool(prefer) and not o.get("force_whisper")

    model = str(o.get("model") or cfg.get("whisper_model") or "")
    if not models.known(model):
        model = cfg.get("whisper_model") if models.known(cfg.get("whisper_model")) else \
            hardware.summary()["recommended_model"]
    try:
        beam = max(1, min(10, int(o.get("beam", cfg.get("whisper_beam", 5)))))
    except (TypeError, ValueError):
        beam = 5
    raw_formats = o.get("formats")
    if not isinstance(raw_formats, (list, tuple)):
        raw_formats = DEFAULT_FORMATS
    formats = [f for f in raw_formats if isinstance(f, str) and f in subs.FORMATTERS]
    exts, unique = set(), []
    for f in formats:
        ext = subs.EXTENSIONS[f]
        if ext not in exts:
            exts.add(ext)
            unique.append(f)
    return {
        "language": language,
        "translate": translate,
        "prefer_captions": prefer,
        "model": model,
        "device": str(o.get("device") or cfg.get("whisper_device") or "auto"),
        "compute": str(o.get("compute") or cfg.get("whisper_compute") or "auto"),
        "beam": beam,
        "vad": bool(o.get("vad", cfg.get("whisper_vad", True))),
        "hotwords": str(o.get("hotwords") or "").strip()[:500],
        "initial_prompt": str(o.get("initial_prompt") or "").strip()[:500],
        "word_timestamps": bool(o.get("word_timestamps")),
        "keep_audio": bool(o.get("keep_audio")),
        "formats": unique or list(DEFAULT_FORMATS),
    }


def _whisper_language(code: str) -> str | None:
    """A language code Whisper accepts, or None to let it detect."""
    p = captions.primary(code)
    p = {"jv": "jw", "fil": "tl", "nb": "no", "iw": "he"}.get(p, p)
    if not p:
        return None
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
    except Exception:
        return p
    return p if p in _LANGUAGE_CODES else None


def english_only(name: str) -> bool:
    cid = models.canonical(name)
    return cid.endswith(".en") or cid.startswith("distil-")


def needs_translation(translate: bool, language: str) -> bool:
    """Translating speech that is already English only costs a model switch
    (and possibly a large download) for the same words, so it is skipped."""
    return bool(translate) and captions.primary(language) != "en"


def pick_model(name: str, translate: bool, language: str,
               installed: set[str] | None = None,
               recommended: str | None = None) -> tuple[str, str]:
    """The model to run, and a plain reason when it differs from the choice.

    Large v3 Turbo and the distil models cannot translate: asked to, they hand
    back the original language. Switching this one job to a model that can,
    and saying so in the step note and the technical detail, beats both
    alternatives: returning untranslated text as if it were English, or
    refusing and making the user find a setting whose effect they cannot
    predict. Large v3 is used when it is already downloaded, since it
    translates best and costs nothing extra; otherwise Medium, the closest in
    quality to what was chosen at the same download size as Turbo. The saved
    model choice is left alone, so the next plain transcript uses it again.
    An English-only model asked for another language is switched the same way,
    to a downloaded multilingual model or else the one this PC is recommended
    (which is sized for its graphics memory).
    """
    if installed is None:
        installed = set()
        for e in models.installed():
            if e["ok"]:
                installed.add(e["name"])
                installed.update(e.get("aliases") or [])
    chosen = models.label(name)
    if translate and not models.can_translate(name):
        pick = "large-v3" if "large-v3" in installed else "medium"
        return pick, f"using {models.label(pick)}, since {chosen} can't translate"
    lang = captions.primary(language)
    if lang and lang != "en" and english_only(name):
        if recommended is None:
            recommended = hardware.summary()["recommended_model"]
        if not models.known(recommended) or english_only(recommended):
            recommended = "small"
        pick = next((m for m in ("large-v3-turbo", "large-v3", "medium", "small")
                     if m in installed), recommended)
        return pick, f"using {models.label(pick)}, since {chosen} only understands English"
    return name, ""


# ------------------------------------------------------------- job plumbing

class _Job:
    """Step and progress bookkeeping for one transcript job.

    The model download reports from its own thread. Every change of step and
    every progress write therefore happens under one lock, and a report for a
    step that is already over is dropped, so a late download tick can never
    turn a finished or cancelled step back to "active".
    """

    def __init__(self, jid: str):
        self.jid = jid
        self.current = ""                   # the step that is active now
        self._last = 0.0
        self._floor = 0.0                   # the bar never moves back within a step
        self._lock = threading.Lock()

    def check(self) -> None:
        jobs.raise_if_cancelled(self.jid)

    def begin(self, key: str, stage: str, label: str | None = None, note: str | None = None,
              indeterminate: bool = True) -> None:
        self.check()
        with self._lock:
            self.current = key
            self._floor = 0.0
            jobs.step(self.jid, key, "active", note=note, label=label)
            jobs.update(self.jid, stage=stage, progress=0.0, indeterminate=indeterminate,
                        bytes_done=None, bytes_total=None, speed_bps=None, eta_s=None,
                        speed="", eta="")

    def end(self, key: str, state: str = "done", note: str | None = None,
            label: str | None = None) -> None:
        with self._lock:
            if key == self.current:
                self.current = ""
            jobs.step(self.jid, key, state, note=note, label=label)

    def stop(self, state: str) -> None:
        """Close the step that was running when the job ended early: 'failed'
        for an error, 'skipped' for a cancel, so no step is left spinning."""
        with self._lock:
            if self.current:
                jobs.step(self.jid, self.current, state)
                self.current = ""

    def progress(self, force: bool = False, note: str | None = None, step: str | None = None,
                 **fields) -> bool:
        """Throttled job update (4 a second). note, when given, replaces the
        active step's note in the same beat; step, when given, drops the
        update unless that step is still the active one. Returns whether it
        was sent."""
        now = time.monotonic()
        with self._lock:
            if step is not None and step != self.current:
                return False
            if not force and now - self._last < 0.25:
                return False
            self._last = now
            if fields.get("indeterminate"):
                self._floor = 0.0           # a new phase inside the step (decoding)
            elif isinstance(fields.get("progress"), (int, float)):
                # Size estimates shift while a download runs; the bar still
                # only ever moves forward.
                self._floor = max(self._floor, float(fields["progress"]))
                fields["progress"] = self._floor
            jobs.update(self.jid, **fields)
            if note is not None and self.current:
                jobs.step(self.jid, self.current, "active", note=note)
        return True


def _mb_note(done: int, total: int) -> str:
    """'812 of 1,550 MB', the model step's note while it downloads."""
    mb = 1048576
    if total:
        return f"{done / mb:,.0f} of {total / mb:,.0f} MB"
    return f"{done / mb:,.0f} MB"


def _fmt_eta(seconds) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _fmt_speed(bps) -> str:
    if not bps:
        return ""
    val, units, i = float(bps), ["B/s", "KB/s", "MB/s", "GB/s"], 0
    while val >= 1024 and i < 3:
        val, i = val / 1024, i + 1
    return f"{val:.1f} {units[i]}"


def _site(url: str) -> str:
    if not url:
        return ""
    try:
        from .errors import site_name
        return site_name(url)
    except Exception:
        return ""


def _upload_root() -> Path:
    return Path(tempfile.gettempdir()) / "media-toolkit"


def _is_upload_copy(path: str) -> bool:
    """True only for the temporary copy the upload endpoint made, never for a
    file of the user's own."""
    try:
        root = _upload_root().resolve()
        return root in Path(path).resolve().parents
    except OSError:
        return False


def _drop_upload_copy(path: str) -> None:
    """Delete the upload's temporary copy, and the folder the upload endpoint
    made for it once that is empty. Files outside the upload area are the
    user's own and are never touched."""
    if not _is_upload_copy(path):
        return
    copy = Path(path)
    try:
        copy.unlink(missing_ok=True)
    except OSError:
        return
    folder = copy.parent
    try:
        if folder.resolve() != _upload_root().resolve() and \
                folder.resolve() != Path(getattr(jobs, "UPLOAD_ROOT", folder)).resolve():
            folder.rmdir()                      # only succeeds when empty
    except OSError:
        pass


def _strip_ext(name: str) -> str:
    m = re.match(r"^(.*\S)\.([A-Za-z0-9]{1,5})$", name or "")
    return m.group(1) if m else (name or "")


# ----------------------------------------------------------------- captions

@contextmanager
def _ydl(opts: dict):
    """A YoutubeDL for these options that cleans up after itself.

    media.base_opts() hands each caller a private copy of the user's cookie
    file; it holds live sign-in sessions, so it is deleted as soon as this
    instance is done instead of waiting in the temp folder.
    """
    from yt_dlp import YoutubeDL
    from . import media

    try:
        with YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        release = getattr(media, "release", None)
        if release:
            try:
                release(opts)
            except Exception:
                pass


# Why a link cannot be transcribed yet. A job for it ends as "skipped" with
# this as its status line, like a live stream pasted on the Download tab.
LIVE_NOW = "This is a live stream. Transcripts need a finished video."
LIVE_LATER = "This video hasn't started yet. Transcripts need a finished video."


def _extract(url: str, translate: bool) -> dict:
    """One look at the link: metadata, caption lists and audio formats.

    No format is chosen here, and a video whose formats cannot be listed is
    not an error yet: its captions may still be there. Picking the audio
    stream is left to the audio download, which only runs without captions.
    """
    from . import media

    opts = media.base_opts()
    opts.update({"skip_download": True, "noplaylist": True,
                 "extract_flat": "in_playlist", "ignore_no_formats_error": True,
                 # A playlist or channel is refused, so one entry is enough to
                 # tell; listing a whole channel first took about a minute.
                 "playlistend": 1,
                 # Translations of the uploader's captions are only listed on request.
                 "writesubtitles": True, "writeautomaticsub": bool(translate)})
    for key in ("outtmpl", "paths", "postprocessors", "download_archive", "format",
                "match_filter", "download_ranges", "max_downloads", "playlist_items"):
        opts.pop(key, None)
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise AppError("unavailable", f"Nothing could be read from {url}")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        # The title fills in the job card, so it never shows only the link.
        raise AppError("playlist_not_supported",
                       f"{url} is a playlist or channel ({info.get('title') or ''})",
                       title=info.get("title") or "",
                       uploader=info.get("uploader") or info.get("channel") or "")
    return info


def _unfinished(info: dict) -> str:
    """Why this video cannot be transcribed yet, or ''."""
    if info.get("is_live") or info.get("live_status") == "is_live":
        return LIVE_NOW
    if info.get("live_status") == "is_upcoming":
        return LIVE_LATER
    return ""


_URL = re.compile(r"https?://\S+")


def _short_error(exc: BaseException) -> str:
    """An exception as one log-safe line: caption URLs carry signed tokens."""
    text = _URL.sub("<url>", str(exc) or type(exc).__name__)
    return f"{type(exc).__name__}: {' '.join(text.split())[:200]}"


def _find_captions(url: str, info: dict, language: str, translate: bool, check=None):
    """Walk the ranked caption tracks, best first, until one yields text.

    Returns (choice, segments, note, error). A track that is rate-limited,
    fails or comes back empty moves on to the next; if every candidate
    fails, error says why, and the note tells the user the speech is being
    transcribed instead.
    """
    from . import media

    ranked = captions.candidates(info, language, translate)
    if not ranked:
        return None, [], captions.note_for(None, language, info, translate), ""
    problems: list[str] = []
    opts = media.base_opts()
    opts.update({"skip_download": True})
    with _ydl(opts) as ydl:
        for choice in ranked:
            if check:
                check()
            try:
                segs = captions.fetch(ydl, choice)
            except jobs.Cancelled:
                raise
            except Exception as exc:        # noqa: BLE001  try the next track
                problems.append(f"{choice.key}: {_short_error(exc)}")
                continue
            if segs:
                return choice, segs, captions.note_for(choice), ""
            problems.append(f"{choice.key}: no text")
    error = "; ".join(problems)[:600]
    print(f"transcribe: captions failed, using speech recognition ({error})", flush=True)
    return None, [], NOTE_CAPTIONS_FAILED, error


# -------------------------------------------------------------------- audio

def _download_audio(url: str, info: dict, workdir: Path, hook, check=None) -> Path:
    """Fetch the smallest usable audio stream into this job's own folder.

    Reuses the metadata already extracted instead of asking the site twice,
    falling back to a fresh extraction if that fails. Each job gets its own
    folder, so two jobs for the same video never share (or delete) one file.
    """
    from . import media

    opts = media.base_opts()
    opts.update({"format": AUDIO_FORMAT, "noplaylist": True,
                 "outtmpl": str(workdir / "audio.%(ext)s"),
                 "progress_hooks": [hook], "overwrites": True,
                 "writesubtitles": False, "writeautomaticsub": False,
                 "writethumbnail": False, "writeinfojson": False,
                 "writedescription": False, "getcomments": False})
    for key in ("paths", "postprocessors", "download_archive", "download_ranges",
                "match_filter", "max_downloads"):
        opts.pop(key, None)
    with _ydl(opts) as ydl:
        try:
            done = ydl.process_ie_result(ydl.sanitize_info(dict(info), True), download=True)
        except jobs.Cancelled:
            raise
        except Exception:
            if check:
                check()
            done = ydl.extract_info(url, download=True)
    for entry in (done or {}).get("requested_downloads") or []:
        if entry.get("filepath") and Path(entry["filepath"]).is_file():
            return Path(entry["filepath"])
    hits = [p for p in workdir.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if not hits:
        raise RuntimeError("The audio download produced no file")
    return hits[0]


# ---------------------------------------------------------------- whisper

def _transcribe(samples: np.ndarray, o: dict, job: _Job | None, language: str,
                language_chosen: bool) -> tuple[list[subs.Segment], dict]:
    """Model step plus transcribing step. Returns (segments, detail)."""
    if not len(samples):
        raise AppError("no_speech", "The audio track is empty")
    requested = o["model"]
    translate = needs_translation(o["translate"], language)
    name, why = pick_model(requested, translate, language)
    jid = job.jid if job else None
    check = job.check if job else None

    need_download = models.local_path(name) is None
    # A model already on disk only needs loading; saying "Downloading" then
    # would be untrue, so the step is renamed for this run.
    model_label = STEP_LABELS["model_download" if need_download else "model_load"]
    downloaded = [0]
    if job:
        job.begin("model", model_label, label=model_label, note=why or None)
        if why:
            jobs.update(jid, stage_detail=f"{name} · instead of {requested}")

        def on_progress(done: int, total: int) -> None:
            # Runs on the download's thread; step="model" drops a late tick.
            downloaded[0] = total or done
            job.progress(step="model", stage=STEP_LABELS["model_download"], bytes_done=done,
                         bytes_total=total or None, indeterminate=not total,
                         progress=(done / total) if total else 0.0,
                         note=_mb_note(done, total))
    else:
        on_progress = None

    models.ensure(name, on_progress=on_progress, check=check)

    def on_wait() -> None:
        if job:
            job.progress(force=True, stage=STAGE_WAITING, indeterminate=True, progress=0.0)

    with _whisper_slot(check, on_wait):
        if job:
            job.progress(force=True, stage=STEP_LABELS["model_load"], indeterminate=True,
                         progress=0.0, bytes_done=None, bytes_total=None)
        model, device, compute = load_model(name, o["device"], o["compute"], check=check)
        detail_line = f"{name} · {device.upper()} {compute}"
        if why:
            detail_line += f" · instead of {requested}"
        step_label = STEP_LABELS["transcribing_cuda" if device == "cuda" else "transcribing_cpu"]
        if job:
            done_note = why or (f"{downloaded[0] / 1048576:,.0f} MB" if downloaded[0] else None)
            job.end("model", "done", note=done_note)
            job.begin("transcribing", step_label, label=step_label, indeterminate=True)
            jobs.update(jid, stage_detail=detail_line)

        total = audio.duration_of(samples)
        lang = _whisper_language(language) if language else None
        kwargs = dict(
            beam_size=o["beam"],
            language=lang,
            task="translate" if translate else "transcribe",
            vad_filter=o["vad"],
            word_timestamps=o["word_timestamps"],
        )
        if o["initial_prompt"]:
            kwargs["initial_prompt"] = o["initial_prompt"]
        if o["hotwords"]:
            kwargs["hotwords"] = o["hotwords"]
        if o["vad"]:
            kwargs["vad_parameters"] = dict(min_silence_duration_ms=500)
            _quiet_onnxruntime()

        started = time.time()
        out: list[subs.Segment] = []
        try:
            generator, info = model.transcribe(samples, **kwargs)
            for seg in generator:
                if job:
                    job.check()
                text = (seg.text or "").strip()
                if text:
                    # Whisper can place the last timestamp past the end of the
                    # audio; subtitle players reject cues beyond the media.
                    end = min(seg.end, total) if total else seg.end
                    start = min(seg.start, end)
                    out.append(subs.Segment(round(start, 3), round(end, 3), text))
                if job and total:
                    frac = min(seg.end / total, 0.99)
                    elapsed = time.time() - started
                    speed = (seg.end / elapsed) if elapsed > 0.5 else 0
                    remaining = ((total - seg.end) / speed) if speed > 0.05 else None
                    job.progress(progress=frac, indeterminate=False,
                                 eta_s=round(remaining) if remaining else None,
                                 speed=f"{speed:.1f}x realtime" if speed else "",
                                 eta=_fmt_eta(remaining))
        except jobs.Cancelled:
            raise
        except AppError:
            raise
        except Exception as exc:
            low = str(exc).lower()
            if "out of memory" in low:
                unload()
                raise AppError("out_of_memory", str(exc)) from exc
            if device == "cuda":
                # Say "it will use the processor instead" and mean it.
                hardware.mark_cuda_failed()
                unload()
                raise AppError("gpu_failed", str(exc)) from exc
            raise
        finally:
            _schedule_idle_unload()

    if not out:
        # Raised while the transcribing step is still open, so the step list
        # shows where it stopped.
        raise AppError("no_speech", "Whisper returned no text for this audio")
    if job:
        job.end("transcribing")
    elapsed = round(time.time() - started, 1)
    detected = getattr(info, "language", "") or (lang or "")
    detail = {
        "source": "whisper",
        "caption_lang": "",
        "source_lang": detected,
        "language": detected,
        "language_probability": round(getattr(info, "language_probability", 0) or 0, 3),
        "language_chosen": language_chosen,
        "translated": translate,
        "task": kwargs["task"],
        "engine": f"Whisper {name}",
        "model": name,
        "model_label": models.label(name),
        "requested_model": requested,
        "model_note": why,
        "device": device,
        "compute_type": compute,
        "audio_duration": round(total, 2),
        "elapsed": elapsed,
        "realtime_factor": round(total / elapsed, 1) if elapsed > 0 and total else None,
    }
    return out, detail


def transcribe_file(path: str, o: dict | None = None) -> tuple[list[subs.Segment], dict]:
    """Transcribe a local file outside the job system (scripts and tests)."""
    opts = _options(o or {}, config.get())
    samples = audio.decode(path)
    return _transcribe(samples, opts, None, opts["language"], bool(opts["language"]))


# --------------------------------------------------------------- job wrapper

def run_transcript(jid: str, url: str, o: dict) -> dict:
    """Worker body: the site's own captions when they fit the request,
    otherwise the speech is transcribed on this PC."""
    o = dict(o or {})
    cfg = config.get()
    opts = _options(o, cfg)
    job = _Job(jid)
    local = o.get("local_path") or ""
    chosen = bool(opts["language"])

    steps = [("reading", STEP_LABELS["reading_file" if local else "reading"])]
    if not local and opts["prefer_captions"]:
        steps.append(("captions", STEP_LABELS["captions"]))
    if not local:
        steps.append(("audio", STEP_LABELS["audio"]))
    steps += [("model", STEP_LABELS["model_download"]),
              ("transcribing", STEP_LABELS["transcribing"]),
              ("saving", STEP_LABELS["saving"])]
    jobs.set_steps(jid, steps)

    started = time.time()
    workdir = _upload_root() / f"{jid}_{uuid.uuid4().hex[:8]}"
    audio_file: Path | None = None
    segments: list[subs.Segment] = []
    detail: dict = {}
    meta: dict

    try:
        if local:
            # The job title is the file's original name; the copy on disk has a
            # unique temporary name.
            given = str((jobs.get(jid) or {}).get("title") or "")
            title = _strip_ext(given if given and given != local else Path(local).name)
            meta = {"title": title, "url": "", "uploader": "", "duration": 0,
                    "duration_string": "", "thumbnail": "", "language": ""}
            job.begin("reading", STEP_LABELS["reading_file"], indeterminate=True)
            jobs.update(jid, title=title)
            samples = audio.decode(
                local, check=job.check,
                on_progress=lambda f: job.progress(progress=f, indeterminate=False))
            meta["duration"] = round(audio.duration_of(samples), 2)
            meta["duration_string"] = captions._dur(meta["duration"])
            job.end("reading")
            language = opts["language"]
            segments, detail = _transcribe(samples, opts, job, language, chosen)
            del samples
        else:
            job.begin("reading", STEP_LABELS["reading"], indeterminate=True)
            info = _extract(url, opts["translate"])
            meta = captions.meta_from_info(info, url)
            jobs.update(jid, title=meta["title"], thumbnail=meta["thumbnail"],
                        uploader=meta["uploader"])
            if (reason := _unfinished(info)):
                raise jobs.Skipped(reason)
            job.end("reading")
            # The spoken language: what the user picked, else what the site says.
            language = opts["language"] or captions.primary(info.get("language"))

            caption_error = ""
            if opts["prefer_captions"]:
                job.begin("captions", STEP_LABELS["captions"], indeterminate=True)
                cap_started = time.time()
                choice, segments, note, caption_error = _find_captions(
                    url, info, opts["language"], opts["translate"], check=job.check)
                if choice:
                    job.end("captions", "done", note=note)
                    for key in ("audio", "model", "transcribing"):
                        job.end(key, "skipped")
                    detail = {
                        "source": choice.source,
                        "caption_lang": choice.caption_lang,
                        "source_lang": choice.source_lang,
                        "language": choice.caption_lang,
                        "language_chosen": chosen,
                        "translated": choice.source == "auto_translated",
                        "task": "translate" if opts["translate"] else "transcribe",
                        "engine": "captions",
                        "caption_key": choice.key,
                        "device": "",
                        "compute_type": "",
                        "elapsed": round(time.time() - cap_started, 1),
                        "realtime_factor": None,
                    }
                else:
                    # Looking was done either way; the note says what was found
                    # (or that loading failed), and the speech is transcribed.
                    job.end("captions", "done", note=note)

            if not segments:
                workdir.mkdir(parents=True, exist_ok=True)
                job.begin("audio", STEP_LABELS["audio"], indeterminate=True)

                def hook(d: dict) -> None:
                    job.check()
                    if d.get("status") != "downloading":
                        return
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    got = d.get("downloaded_bytes") or 0
                    job.progress(progress=min(got / total, 0.99) if total else 0.0,
                                 indeterminate=not total, bytes_done=got,
                                 bytes_total=total or None,
                                 speed_bps=d.get("speed") or None,
                                 eta_s=d.get("eta"), speed=_fmt_speed(d.get("speed")),
                                 eta=_fmt_eta(d.get("eta")))

                audio_file = _download_audio(url, info, workdir, hook, check=job.check)
                job.progress(force=True, progress=0.0, indeterminate=True,
                             bytes_done=None, bytes_total=None, speed_bps=None,
                             eta_s=None, speed="", eta="")
                samples = audio.decode(str(audio_file), check=job.check)
                job.end("audio")
                segments, detail = _transcribe(samples, opts, job, language, chosen)
                del samples
                if caption_error:
                    detail["caption_error"] = caption_error

        if not segments:
            raise AppError("no_speech", "Whisper returned no text for this audio")

        job.begin("saving", STEP_LABELS["saving"], indeterminate=True)
        meta.setdefault("url", url)
        detail["site"] = _site(meta.get("url") or url)
        detail["total_elapsed"] = round(time.time() - started, 1)
        result = _save(jid, segments, meta, detail, opts, cfg, audio_file)
        job.end("saving")
    except BaseException as exc:
        stopped = isinstance(exc, (jobs.Cancelled, jobs.Skipped)) or jobs.cancelled(jid)
        job.stop("skipped" if stopped else "failed")
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # The upload's temporary copy is only needed for a retry after a failure.
    if local:
        _drop_upload_copy(local)
    return result


# ------------------------------------------------------------------ saving

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


# Characters Windows refuses in file names, replaced the way a person would
# write the title by hand: "Part 1: Intro?" becomes "Part 1 - Intro".
_NAME_SWAPS = str.maketrans({":": " - ", "/": "-", "\\": "-", "|": "-", '"': "'",
                             "?": "", "*": "", "<": "", ">": ""})


def _safe_stem(title: str, limit: int = 120) -> str:
    out = "".join(" " if ord(c) < 32 else c for c in (title or "")).translate(_NAME_SWAPS)
    out = " ".join(out.split()).strip(" .")
    out = out[:limit].strip(" .")
    if out.split(".")[0].upper() in _RESERVED:
        out = f"_{out}"
    return out or "transcript"


def unique_stem(outdir: Path, title: str) -> str:
    """One name for every file of a transcript: 'Title', then 'Title (2)'...

    A number is used as soon as any of the transcript's files would clash, so
    the .txt, .srt and .md of one transcript always share the same name.
    """
    base = _safe_stem(title)
    n = 1
    while True:
        stem = base if n == 1 else f"{base} ({n})"
        if not any((outdir / f"{stem}.{ext}").exists() for ext in TRANSCRIPT_EXTS):
            return stem
        n += 1


FILE_KINDS = {"txt": "transcript", "srt": "subtitles", "vtt": "subtitles",
              "md": "notes", "json": "json"}


def _save(jid: str, segments: list[subs.Segment], meta: dict, detail: dict, opts: dict,
          cfg: dict, audio_file: Path | None) -> dict:
    # Folders come from Settings only, never from the request.
    outdir = Path(cfg["transcript_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    stats = subs.stats(segments)
    rendered: dict[str, str] = {}
    written: list[Path] = []
    with _save_lock:
        stem = unique_stem(outdir, meta.get("title") or "transcript")
        for fmt in opts["formats"]:
            body = subs.render(segments, fmt, meta)
            rendered[fmt] = body
            path = outdir / f"{stem}.{subs.EXTENSIONS.get(fmt, 'txt')}"
            path.write_text(body, encoding="utf-8")
            written.append(path)
        sidecar = outdir / f"{stem}.mt.json"
        payload = {"version": 1, "stem": stem, "created": time.time(),
                   "formats": list(rendered.keys()), "meta": meta, "detail": detail,
                   "stats": stats, "segments": [s.to_dict() for s in segments]}
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, sidecar)

    for path in written:
        ext = path.suffix.lstrip(".").lower()
        jobs.add_file(jid, str(path), FILE_KINDS.get(ext, ""))

    if audio_file is not None and opts["keep_audio"] and audio_file.is_file():
        dest_dir = Path(cfg["download_dir"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = _safe_stem(meta.get("title") or "audio")
        dest = dest_dir / f"{base}{audio_file.suffix}"
        n = 2
        while dest.exists():
            dest = dest_dir / f"{base} ({n}){audio_file.suffix}"
            n += 1
        shutil.move(str(audio_file), dest)
        jobs.add_file(jid, str(dest), "audio")

    return {
        "meta": meta,
        "detail": detail,
        "stats": stats,
        "formats": list(rendered.keys()),
        "output_dir": str(outdir),
        "stem": stem,
        "sidecar": str(sidecar),
        "text": rendered.get("txt") or subs.render(segments, "txt", meta),
        "segments": [s.to_dict() for s in segments],
    }
