"""Speech recognition via faster-whisper, plus the transcript job orchestrator.

Backend selection is proven, not assumed: candidate (device, compute_type) pairs
come from hardware.py and each one is loaded and warmed up on real audio before
we trust it. The first pair that survives is cached, so later runs start instantly.
A machine with no GPU, an old GPU, or a broken CUDA install still ends up on a
working CPU backend rather than an error.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from threading import Lock

import numpy as np

from . import config, hardware, jobs, media, models, subs

_models: dict[tuple, object] = {}
_load_lock = Lock()

MODELS = [
    {"id": "tiny", "label": "Tiny", "note": "fastest, roughest"},
    {"id": "base", "label": "Base", "note": "quick drafts"},
    {"id": "small", "label": "Small", "note": "good CPU default"},
    {"id": "medium", "label": "Medium", "note": "strong accuracy"},
    {"id": "distil-large-v3", "label": "Distil Large v3", "note": "near-large, ~2x faster, English"},
    {"id": "large-v3-turbo", "label": "Large v3 Turbo", "note": "best speed/accuracy balance"},
    {"id": "large-v3", "label": "Large v3", "note": "most accurate"},
]


def model_catalog() -> list[dict]:
    hw = hardware.summary()
    vram = hw["vram_mb"] if hw["cuda"] else 0
    out = []
    for m in MODELS:
        need = hardware.estimated_vram(m["id"], "int8_float16" if vram else "int8")
        out.append({**m, "fits": (vram == 0 or need <= vram),
                    "vram_mb": hardware.MODEL_VRAM.get(m["id"], 0)})
    return out


def _warmup(model) -> None:
    """Force the encoder to actually run so a broken backend fails here, loudly,
    instead of halfway through a real transcription."""
    audio = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32)
    segments, _ = model.transcribe(audio, beam_size=1, vad_filter=False, language="en")
    for _ in segments:
        break


def load_model(name: str, device_pref: str = "auto", compute_pref: str = "auto",
               on_status=None) -> tuple[object, str, str]:
    """Return (model, device, compute_type), trying candidates until one works."""
    from faster_whisper import WhisperModel

    with _load_lock:
        cached = hardware.recall(name) if (device_pref == "auto" and compute_pref == "auto") else None
        order = hardware.candidates(name, device_pref, compute_pref)
        if cached and tuple(cached) in [tuple(c) for c in order]:
            order = [tuple(cached)] + [c for c in order if tuple(c) != tuple(cached)]

        errors: list[str] = []

        # Fetch and verify the weights once, before trying any backend. Doing it
        # here means a broken cache is repaired instead of failing identically
        # for every candidate, and the download can report progress.
        def progress(done: int, total: int):
            if on_status:
                on_status(f"Downloading {name}: {done / 1048576:.0f} of "
                          f"{total / 1048576:.0f} MB", done / total if total else 0.0)

        model_path = models.ensure(
            name,
            on_status=(lambda t: on_status(t, 0.0)) if on_status else None,
            on_progress=progress if on_status else None,
        )

        for device, compute in order:
            key = (name, device, compute)
            if key in _models:
                return _models[key], device, compute
            try:
                if on_status:
                    on_status(f"Loading {name} on {device.upper()} ({compute})", None)
                model = WhisperModel(
                    model_path, device=device, compute_type=compute,
                    cpu_threads=min(16, hardware.summary()["cpu_threads"]) if device == "cpu" else 0,
                    num_workers=1,
                )
                _warmup(model)
            except Exception as exc:
                text = str(exc)
                if "unable to open file" in text.lower():
                    # The weights are corrupt, not the backend. Repair once and retry.
                    models.purge(name)
                    raise RuntimeError(
                        f"The {name} model files were damaged, so they have been removed. "
                        "Start the transcription again and the app will download them afresh."
                    ) from exc
                errors.append(f"{device}/{compute}: {text[:160]}")
                continue
            _models[key] = model
            if device_pref == "auto" and compute_pref == "auto":
                hardware.remember(name, device, compute)
            return model, device, compute

        raise RuntimeError(
            "No working transcription backend. Tried:\n  " + "\n  ".join(errors)
            + "\nSet Device to CPU in Settings if this keeps happening."
        )


def transcribe_file(path: str, jid: str | None = None, o: dict | None = None,
                    duration_hint: float = 0.0) -> tuple[list[subs.Segment], dict]:
    """Run Whisper over an audio/video file and return (segments, info)."""
    o = o or {}
    cfg = config.get()
    name = o.get("model") or cfg["whisper_model"]

    def status(text: str, progress: float | None = 0.0):
        if not jid:
            return
        if progress is None:
            jobs.update(jid, stage=text)
        else:
            jobs.update(jid, stage=text, progress=progress)

    model, device, compute = load_model(
        name, o.get("device") or cfg["whisper_device"],
        o.get("compute") or cfg["whisper_compute"], on_status=status)

    status(f"Transcribing on {device.upper()} ({compute}, {name})")
    lang = (o.get("language") or "").strip() or None
    kwargs = dict(
        beam_size=int(o.get("beam", cfg["whisper_beam"])),
        language=None if lang in (None, "auto") else lang,
        task="translate" if o.get("translate") else "transcribe",
        vad_filter=bool(o.get("vad", cfg["whisper_vad"])),
        word_timestamps=bool(o.get("word_timestamps")),
        condition_on_previous_text=bool(o.get("condition", True)),
    )
    if o.get("initial_prompt"):
        kwargs["initial_prompt"] = o["initial_prompt"]
    if o.get("hotwords"):
        kwargs["hotwords"] = o["hotwords"]
    if kwargs["vad_filter"]:
        kwargs["vad_parameters"] = dict(min_silence_duration_ms=500)

    started = time.time()
    generator, info = model.transcribe(path, **kwargs)
    total = getattr(info, "duration", 0) or duration_hint or 0

    out: list[subs.Segment] = []
    for seg in generator:
        if jid:
            jobs.raise_if_cancelled(jid)
        text = (seg.text or "").strip()
        if text:
            out.append(subs.Segment(seg.start, seg.end, text))
        if jid and total:
            frac = min(seg.end / total, 0.99)
            elapsed = time.time() - started
            speed = (seg.end / elapsed) if elapsed > 0.5 else 0
            remaining = ((total - seg.end) / speed) if speed > 0.05 else 0
            jobs.update(jid, progress=frac,
                        speed=f"{speed:.1f}x realtime" if speed else "",
                        eta=media._fmt_eta(remaining) if remaining else "")

    detail = {
        "engine": f"Whisper {name}",
        "device": device,
        "compute_type": compute,
        "language": getattr(info, "language", lang or ""),
        "language_probability": round(getattr(info, "language_probability", 0) or 0, 3),
        "audio_duration": total,
        "elapsed": round(time.time() - started, 1),
    }
    if detail["elapsed"] > 0 and total:
        detail["realtime_factor"] = round(total / detail["elapsed"], 1)
    return out, detail


# --------------------------------------------------------------- job wrapper

def run_transcript(jid: str, url: str, o: dict) -> dict:
    """Worker body: captions if the site has them, otherwise Whisper."""
    cfg = config.get()
    langs = [s.strip() for s in (o.get("langs") or cfg["subtitle_langs"]).split(",") if s.strip()] or ["en"]
    meta: dict = {}
    segments: list[subs.Segment] = []
    detail: dict = {}
    local = o.get("local_path")

    if local:
        path = local
        meta = {"title": Path(local).stem, "url": "", "uploader": ""}
        jobs.update(jid, title=meta["title"], stage="Reading local file")
        segments, detail = transcribe_file(path, jid, o)
    else:
        if cfg["prefer_native_subs"] and not o.get("force_whisper"):
            jobs.update(jid, stage="Checking for existing captions", progress=0.05)
            try:
                found = media.fetch_captions(url, langs)
            except Exception:
                found = None
            if found:
                segments, meta, label = found
                detail = {"engine": label, "device": "-", "elapsed": 0}
                jobs.update(jid, title=meta.get("title", url), progress=0.9,
                            thumbnail=meta.get("thumbnail", ""), stage=label)

        if not segments:
            jobs.update(jid, stage="Downloading audio", progress=0.05)
            tmp = media.temp_dir()

            def hook(d):
                jobs.raise_if_cancelled(jid)
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    got = d.get("downloaded_bytes") or 0
                    if total:
                        jobs.update(jid, progress=0.05 + 0.15 * (got / total),
                                    speed=media._fmt_speed(d.get("speed")))

            path, meta = media.download_audio(url, tmp, hook)
            jobs.update(jid, title=meta.get("title", url), thumbnail=meta.get("thumbnail", ""))
            try:
                segments, detail = transcribe_file(path, jid, o, meta.get("duration", 0))
            finally:
                if not o.get("keep_audio"):
                    Path(path).unlink(missing_ok=True)
                else:
                    dest = Path(cfg["download_dir"]) / Path(path).name
                    shutil.move(path, dest)
                    jobs.add_file(jid, str(dest), "audio")

    if not segments:
        raise RuntimeError(
            "No speech found. The video may have no audible speech, or VAD filtered "
            "everything out -- try turning VAD off in Settings."
        )

    meta.setdefault("url", url)
    outdir = Path(o.get("output_dir") or cfg["transcript_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(meta.get("title") or "transcript")

    formats = o.get("formats") or ["txt", "srt", "md"]
    rendered: dict[str, str] = {}
    for fmt in formats:
        body = subs.render(segments, fmt, meta)
        rendered[fmt] = body
        path = outdir / f"{stem}.{subs.EXTENSIONS.get(fmt, 'txt')}"
        if path.exists():
            path = outdir / f"{stem} ({int(time.time())}).{subs.EXTENSIONS.get(fmt, 'txt')}"
        path.write_text(body, encoding="utf-8")
        jobs.add_file(jid, str(path), fmt)

    stats = subs.stats(segments)
    return {
        "meta": meta,
        "detail": detail,
        "stats": stats,
        "text": rendered.get("txt") or subs.render(segments, "txt", meta),
        "formats": list(rendered.keys()),
        "segments": [s.to_dict() for s in segments],
        "output_dir": str(outdir),
    }


def _safe_stem(title: str, limit: int = 120) -> str:
    bad = '<>:"/\\|?*'
    out = "".join(("-" if c in bad else c) for c in title).strip(" .")
    out = " ".join(out.split())
    return (out[:limit].strip() or "transcript")
