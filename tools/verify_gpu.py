"""Check that a frozen build transcribes on an NVIDIA card with only the GPU pack.

    python tools/verify_gpu.py [dist\\MediaToolkit] [--home DIR] [--audio FILE]

Starts the built MediaToolkit.exe without a window (--server) on a free port
and a scratch data folder, installs the GPU pack through the app's own API if
that folder does not have it yet (a 528 MB download from PyPI), transcribes a
short spoken sample with the tiny model, and checks three things:

* the job really ran on CUDA;
* the only NVIDIA libraries in the process are the driver and the pack's
  cuBLAS: no cuDNN, nothing NVIDIA from the build folder;
* no GPL codec library (x264, x265, PyAV's FFmpeg DLLs) was loaded.

This is the measurement behind leaving cudnn64_9.dll out of MediaToolkit.spec,
so run it after upgrading CTranslate2 or the pinned cuBLAS. It needs an NVIDIA
card and driver, never touches the real data folder, stops the app when done
and deletes a temporary data folder (keep one with --home to skip the pack
download next time). Without --audio it makes the sample with Windows' own
speech synthesizer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_TEXT = ("Media Toolkit is checking that speech recognition runs on the graphics card. "
               "The quick brown fox jumps over the lazy dog. One, two, three, four, five.")
GPL_DLLS = re.compile(r"(libx264|libx265|avcodec|avformat|avutil|swresample)[^\\/]*\.dll$", re.I)
NVIDIA_DLLS = re.compile(r"(cudnn|cublas|cudart|nvrtc|cufft|curand)[^\\/]*\.dll$", re.I)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """The frozen app on a port, driven over its HTTP API."""

    def __init__(self, exe: Path, home: Path, port: int = 0):
        self.port = port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, MEDIA_TOOLKIT_HOME=str(home))
        self.proc = subprocess.Popen([str(exe), "--server", "--port", str(self.port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.token = ""

    def request(self, path: str, data: bytes | None = None, headers: dict | None = None,
                timeout: float = 30) -> bytes:
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        if data is not None:
            req.add_header("X-MT-Token", self.token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def json(self, path: str, data: bytes | None = None, headers: dict | None = None,
             timeout: float = 30):
        return json.loads(self.request(path, data, headers, timeout))

    def wait_ready(self, seconds: float = 90) -> dict:
        end = time.time() + seconds
        while time.time() < end:
            if self.proc.poll() is not None:
                raise SystemExit(f"the app exited at start (code {self.proc.returncode})")
            try:
                about = self.json("/api/about", timeout=3)
                page = self.request("/", timeout=5).decode("utf-8", "replace")
                m = re.search(r'name="mt-token"\s+content="([^"]+)"', page)
                self.token = m.group(1) if m else ""
                return about
            except OSError:
                time.sleep(1)
        raise SystemExit("the app did not answer within 90 s")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def speech_sample(dest: Path) -> Path:
    """A few seconds of speech from Windows' built-in synthesizer."""
    quoted = str(dest).replace("'", "''")               # PowerShell single-quoted string
    script = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{quoted}'); $s.Speak('{SAMPLE_TEXT}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, timeout=120)
    return dest


def multipart(path: Path, options: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"options\"\r\n\r\n"
        f"{json.dumps(options)}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".encode() + path.read_bytes() + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def loaded_dlls(pid: int) -> list[str]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-Process -Id {pid}).Modules | ForEach-Object {{ $_.FileName }}"],
        capture_output=True, text=True, timeout=60).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("app", nargs="?", type=Path, default=ROOT / "dist" / "MediaToolkit",
                    help="the built app folder (default: dist\\MediaToolkit)")
    ap.add_argument("--home", type=Path, help="data folder to use and keep (default: a temporary one)")
    ap.add_argument("--audio", type=Path, help="speech file to transcribe instead of a synthesized one")
    ap.add_argument("--model", default="tiny", help="Whisper model (default: tiny)")
    ap.add_argument("--port", type=int, default=0, help="port for the app (default: a free one)")
    args = ap.parse_args(argv)
    if os.name != "nt":
        print("verify_gpu: Windows only")
        return 2

    exe = args.app.resolve() / "MediaToolkit.exe"
    if not exe.is_file():
        print(f"verify_gpu: {exe} not found; build first (python tools/build.py)")
        return 2
    home = args.home.resolve() if args.home else Path(tempfile.mkdtemp(prefix="mt-verify-gpu-"))
    home.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    app = App(exe, home, args.port)
    try:
        about = app.wait_ready()
        print(f"Media Toolkit {about.get('version')} (yt-dlp {about.get('yt_dlp')}) on {app.base}, "
              f"data folder {home}")
        packs = app.json("/api/packs")
        if not packs.get("gpu_name"):
            print("verify_gpu: no NVIDIA card found")
            return 2
        print(f"GPU: {packs['gpu_name']}")
        if not packs.get("gpu_pack_installed"):
            print(f"Installing the GPU pack ({packs.get('gpu_pack_size_mb')} MB)...")
            res = app.json("/api/packs/gpu", data=b"", timeout=3600)
            if not res.get("ok"):
                print("GPU pack failed:", res.get("error"))
                return 1

        sample = args.audio.resolve() if args.audio else speech_sample(home / "verify-gpu-speech.wav")
        body, ctype = multipart(sample, {"model": args.model, "prefer_captions": False})
        job = app.json("/api/transcribe-file", data=body, headers={"Content-Type": ctype},
                       timeout=120)["jobs"][0]
        end = time.time() + 900
        while time.time() < end:
            current = next((j for j in app.json("/api/jobs")["jobs"] if j["id"] == job["id"]), None)
            if current and current["status"] in ("done", "error", "cancelled", "skipped"):
                job = current
                break
            time.sleep(2)
        detail = (job.get("result") or {}).get("detail") or {}
        print(f"Job: {job['status']}, {detail.get('device')} {detail.get('compute_type')}, "
              f"{detail.get('realtime_factor')}x real time")
        if job["status"] != "done":
            problems.append(f"transcription ended {job['status']}: {job.get('message')}")
        else:
            if detail.get("device") != "cuda":
                problems.append(f"transcription ran on {detail.get('device')!r}, not cuda")
            text = app.request(f"/api/jobs/{job['id']}/transcript?format=txt").decode("utf-8", "replace")
            print(f"Heard: {' '.join(text.split())[:160]}")
            if not args.audio and "fox" not in text.lower():
                problems.append("the transcript does not match the spoken sample")

        dlls = loaded_dlls(app.proc.pid)
        pack_dir = os.path.normcase(str(home / "runtime" / "cuda"))
        for dll in dlls:
            name = os.path.normcase(dll)
            if GPL_DLLS.search(name):
                problems.append(f"GPL codec library loaded: {dll}")
            if NVIDIA_DLLS.search(name):
                if "cudnn" in name:
                    problems.append(f"cuDNN loaded: {dll}")
                elif not name.startswith(pack_dir):
                    problems.append(f"NVIDIA library loaded from outside the GPU pack: {dll}")
                else:
                    print(f"  loaded from the pack: {Path(dll).name}")
        if not any("cublas" in os.path.normcase(d) for d in dlls) and detail.get("device") == "cuda":
            problems.append("cuBLAS was not loaded, so the job cannot have used the card")
    finally:
        app.stop()
        if not args.home:
            shutil.rmtree(home, ignore_errors=True)

    for p in problems:
        print("PROBLEM:", p)
    print("OK: GPU transcription works with only the GPU pack's cuBLAS" if not problems
          else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
