"""FastAPI surface: static UI, job API, and an SSE stream for live progress."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (assets, config, cookies, ffmpegtools, folderpick, hardware, jobs,
               live, media, models, subs, transcribe)

config.bootstrap()
config.ensure_dirs()

app = FastAPI(title="Media Toolkit", docs_url="/api/docs")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def no_store(request, call_next):
    """The app window keeps a persistent browser profile, so a cached UI would
    survive an app update and silently run against a newer backend."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


class JobRequest(BaseModel):
    url: str = ""
    kind: str = "download"          # download | transcript
    options: dict = {}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text("utf-8")


# ------------------------------------------------------------------ metadata

@app.get("/api/hardware")
def api_hardware():
    hw = hardware.summary()
    hw["models"] = transcribe.model_catalog()
    hw["python"] = sys.version.split()[0]
    try:
        import yt_dlp
        hw["yt_dlp"] = yt_dlp.version.__version__
    except Exception:
        hw["yt_dlp"] = "?"
    return hw


@app.get("/api/capabilities")
def api_capabilities():
    """Everything optional that this machine can or cannot do."""
    import shutil
    js = next((r for r in ("deno", "node", "bun", "qjs") if shutil.which(r)), "")
    targets = media.impersonate_targets()
    encoders = ffmpegtools.encoder_catalog()
    return {
        "encoders": encoders,
        "hardware": [e["id"] for e in encoders if e["hw"]],
        "qualities": list(ffmpegtools.QUALITY.keys()),
        "impersonate": targets,
        "impersonate_available": bool(targets),
        "browsers": cookies.installed(),
        "js_runtime": js,
        "aria2c": bool(shutil.which("aria2c")),
        "sponsor_categories": media.SPONSOR_CATEGORIES,
        "audio_codecs": list(media.AUDIO_CODECS),
        "quality_presets": list(media.VIDEO_PRESETS.keys()),
    }


@app.get("/api/settings")
def api_get_settings():
    return {"config": config.get(), "defaults": config.DEFAULTS,
            "quality_presets": list(media.VIDEO_PRESETS.keys()),
            "audio_codecs": list(media.AUDIO_CODECS),
            "sponsor_categories": media.SPONSOR_CATEGORIES}


@app.get("/api/formats")
async def api_formats(url: str):
    if not url.strip():
        raise HTTPException(400, "No URL")
    try:
        return await asyncio.to_thread(media.list_formats, url.strip())
    except Exception as exc:
        raise HTTPException(400, jobs._friendly(exc))


# ------------------------------------------------------------- first-run setup

@app.get("/api/setup")
def api_setup_state():
    cfg = config.get()
    hw = hardware.summary()
    return {
        "needed": not cfg["setup_complete"],
        "config": cfg,
        "hardware": hw,
        "suggestions": {
            "download_dir": str(Path.home() / "Videos" / "Media Toolkit"),
            "transcript_dir": str(Path.home() / "Documents" / "Transcripts"),
            "portable_download_dir": str(config.DATA_ROOT / "downloads"),
            "portable_transcript_dir": str(config.DATA_ROOT / "transcripts"),
            "whisper_model": hw["recommended_model"],
        },
    }


@app.post("/api/setup")
def api_setup_save(patch: dict):
    patch = dict(patch or {})
    patch["setup_complete"] = True
    cfg = config.save(patch)
    for key in ("download_dir", "transcript_dir"):
        try:
            Path(cfg[key]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Could not create {cfg[key]}: {exc}")
    return {"config": cfg}


@app.post("/api/pick-folder")
async def api_pick_folder(body: dict):
    """Native folder chooser. Runs in its own process so a Tk event loop can
    never wedge the server thread."""
    start = body.get("path") or str(Path.home())
    try:
        picked = await asyncio.to_thread(folderpick.choose, start)
    except Exception as exc:
        raise HTTPException(500, f"Folder picker unavailable: {exc}")
    return {"path": picked}


@app.get("/api/live-check")
async def api_live_check(url: str):
    """Is this link live right now, and what can be recorded from it?"""
    if not url.strip():
        raise HTTPException(400, "No URL")
    try:
        info = await asyncio.to_thread(live.resolve, url.strip())
    except Exception as exc:
        raise HTTPException(400, jobs._friendly(exc))
    return {
        "is_live": info["is_live"],
        "live_status": info["live_status"],
        "title": info["title"],
        "meta": info["meta"],
        "has_video": bool(info["video"]),
        "has_audio": bool(info["audio"] or info["single"]),
        "height": (info["video"] or {}).get("height", 0),
        "protocol": (info["video"] or info["audio"] or {}).get("protocol", ""),
    }


@app.get("/api/models")
def api_models():
    """Which Whisper models are downloaded, and are they intact?"""
    return {"models": models.installed(), "cache": str(models.cache_root())}


@app.post("/api/models/repair")
def api_models_repair(body: dict):
    """Delete a model so it is fetched fresh next time."""
    name = (body or {}).get("name", "")
    if not name:
        raise HTTPException(400, "No model named")
    try:
        models.purge(name)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return {"ok": True, "name": name}


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

@app.post("/api/cookies/detect")
async def api_cookies_detect():
    return await asyncio.to_thread(cookies.autodetect)


@app.post("/api/cookies/login")
async def api_cookies_login(body: dict):
    url = body.get("url") or "https://www.instagram.com/accounts/login/"
    return await asyncio.to_thread(cookies.start_login_browser, config.DATA_ROOT, url)


@app.post("/api/cookies/harvest")
async def api_cookies_harvest():
    dest = config.DATA_ROOT / "cookies.txt"
    result = await asyncio.to_thread(cookies.harvest_login_cookies, dest)
    if result.get("ok"):
        config.save({"cookies_file": str(dest), "cookies_browser": ""})
    return result


@app.post("/api/cookies/import")
def api_cookies_import(body: dict):
    dest = config.DATA_ROOT / "cookies.txt"
    try:
        result = cookies.import_text(body.get("text", ""), dest)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    config.save({"cookies_file": str(dest), "cookies_browser": ""})
    return result


@app.post("/api/cookies/clear")
def api_cookies_clear():
    (config.DATA_ROOT / "cookies.txt").unlink(missing_ok=True)
    config.save({"cookies_file": "", "cookies_browser": ""})
    return {"ok": True}


@app.post("/api/settings")
def api_set_settings(patch: dict):
    if "whisper_model" in patch or "whisper_device" in patch or "whisper_compute" in patch:
        hardware.forget()          # re-prove the backend after a device change
    return {"config": config.save(patch)}


@app.get("/api/sites")
def api_sites(q: str = ""):
    return media.supported_sites(q)


@app.get("/api/probe")
async def api_probe(url: str):
    if not url.strip():
        raise HTTPException(400, "No URL")
    try:
        return await asyncio.to_thread(media.probe, url.strip())
    except Exception as exc:
        raise HTTPException(400, jobs._friendly(exc))


# ---------------------------------------------------------------------- jobs

@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.all_jobs(), "version": jobs.version()}


@app.post("/api/jobs")
def api_create(req: JobRequest):
    urls = [u.strip() for u in req.url.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        raise HTTPException(400, "No URL provided")
    created = []
    for url in urls:
        job = jobs.create(req.kind, url, req.options)
        if req.kind == "transcript":
            jobs.submit(job["id"], transcribe.run_transcript, url, req.options)
        elif req.kind == "live":
            jobs.submit(job["id"], live.run_live, url, req.options)
        else:
            jobs.submit(job["id"], media.run_download, url, req.options)
        created.append(job)
    return {"jobs": created}


@app.post("/api/transcribe-file")
async def api_transcribe_file(file: UploadFile = File(...), options: str = Form("{}")):
    opts = json.loads(options or "{}")
    dest = Path(media.temp_dir()) / file.filename
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    opts["local_path"] = str(dest)
    job = jobs.create("transcript", file.filename, opts, title=file.filename)
    jobs.submit(job["id"], transcribe.run_transcript, "", opts)
    return {"jobs": [job]}


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    return {"cancelled": jobs.cancel(jid)}


@app.post("/api/jobs/clear")
def api_clear():
    return {"removed": jobs.clear_finished()}


@app.get("/api/jobs/{jid}/transcript")
def api_transcript(jid: str, format: str = "txt", chunk_size: int = 0):
    job = jobs.get(jid)
    if not job or not job.get("result"):
        raise HTTPException(404, "No transcript on that job")
    result = job["result"]
    segs = [subs.Segment(**s) for s in result.get("segments", [])]
    if not segs:
        raise HTTPException(404, "No segments")
    body = subs.render(segs, format, result.get("meta", {}))
    if chunk_size:
        return JSONResponse({"chunks": subs.chunk(body, chunk_size)})
    return PlainTextResponse(body)


@app.get("/api/file")
def api_file(path: str, download: bool = True):
    p = Path(path)
    allowed = [Path(config.get()["download_dir"]).resolve(),
               Path(config.get()["transcript_dir"]).resolve()]
    try:
        rp = p.resolve(strict=True)
    except OSError:
        raise HTTPException(404, "File not found")
    if not any(rp == a or a in rp.parents for a in allowed):
        raise HTTPException(403, "Outside the download and transcript folders")
    return FileResponse(rp, filename=rp.name if download else None)


@app.post("/api/reveal")
def api_reveal(body: dict):
    """Open a file or folder in the OS file manager."""
    target = Path(body.get("path", "")).resolve()
    if not target.exists():
        raise HTTPException(404, "Nothing there")
    try:
        if os.name == "nt":
            if target.is_dir():
                os.startfile(str(target))          # noqa: S606
            else:
                subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            cmd = ["open"] + (["-R"] if target.is_file() else []) + [str(target)]
            subprocess.Popen(cmd)
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return {"ok": True}


@app.post("/api/update-ytdlp")
async def api_update():
    """Extractors break when sites change; this pulls the newest yt-dlp."""
    def run():
        return subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--no-input", "yt-dlp[default]"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    proc = await asyncio.to_thread(run)
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    return {"ok": proc.returncode == 0, "output": "\n".join(tail) or (proc.stderr or "")[-600:],
            "restart_required": True}


# ----------------------------------------------------------------------- SSE

# Liveness. The UI posts a heartbeat every few seconds; run.py quits once those
# stop. A positive signal beats inferring a closed window from a dropped SSE
# stream, which uvicorn may not notice until its next write.
_clients = 0
_clients_lock = threading.Lock()
_last_seen = time.time()
_seen_any = False


def client_state() -> tuple[int, float, bool]:
    with _clients_lock:
        return _clients, _last_seen, _seen_any


@app.post("/api/heartbeat")
def api_heartbeat():
    global _last_seen, _seen_any
    with _clients_lock:
        _last_seen = time.time()
        _seen_any = True
    return {"ok": True}


@app.post("/api/goodbye")
def api_goodbye():
    """Sent by the page as it unloads, so closing the window quits immediately
    instead of waiting out the heartbeat timeout."""
    global _last_seen
    with _clients_lock:
        _last_seen = 0.0
    return {"ok": True}


@app.get("/api/events")
async def api_events():
    global _clients, _last_seen
    with _clients_lock:
        _clients += 1
        _last_seen = time.time()

    async def stream():
        global _clients, _last_seen
        last = -1
        idle = 0
        try:
            while True:
                current = jobs.version()
                if current != last:
                    last = current
                    payload = json.dumps({"jobs": jobs.all_jobs(), "version": current})
                    yield f"data: {payload}\n\n"
                    idle = 0
                else:
                    idle += 1
                    if idle >= 30:        # keepalive so proxies do not close us
                        idle = 0
                        yield ": ping\n\n"
                with _clients_lock:
                    _last_seen = time.time()
                await asyncio.sleep(0.4)
        finally:
            with _clients_lock:
                _clients -= 1
                _last_seen = time.time()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
