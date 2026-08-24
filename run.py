"""Launcher.

Starts the local server, opens a frameless Chromium app window against it, and
shuts everything down when that window closes -- so it behaves like a desktop
app rather than a web page you have to remember to quit.

Closing the window is the quit signal. It is detected two ways, because a
Chromium launch can hand off to an existing process and exit immediately:
the browser process ending, and the UI's live event stream going away.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# How long to keep running after the last window disappears. Covers a page
# reload, which briefly drops the event stream.
GRACE_SECONDS = 12


def free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 0  # let the OS choose


def wait_for_server(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def diagnose(name: str) -> None:
    """Print exactly what happens when this build downloads a model.

    Packaged builds can behave differently from a source checkout, so this runs
    the real download path inside the real environment and reports every step.
    """
    import os
    import traceback
    from app import config, models
    config.bootstrap()

    print(f"frozen        : {getattr(sys, 'frozen', False)}")
    print(f"cache root    : {models.cache_root()}")
    try:
        import certifi
        print(f"certifi       : {certifi.where()} (exists={os.path.exists(certifi.where())})")
    except Exception as exc:
        print(f"certifi       : unavailable ({exc})")
    try:
        import hf_xet  # noqa: F401
        print("hf_xet        : importable")
    except Exception as exc:
        print(f"hf_xet        : NOT importable ({type(exc).__name__}: {exc})")

    repo = models.repo_id(name)
    print(f"repo          : {repo}")
    models.purge(name)

    from huggingface_hub import HfApi, hf_hub_download
    try:
        siblings = [s.rfilename for s in (HfApi().model_info(repo).siblings or [])]
        print(f"listing       : {siblings}")
    except Exception:
        print("listing       : FAILED")
        traceback.print_exc()
        siblings = []

    for fname in [f for f in siblings if models.wanted(f)]:
        try:
            got = hf_hub_download(repo, fname, cache_dir=str(models.cache_root()))
            size = os.path.getsize(got) if os.path.exists(got) else -1
            print(f"  {fname:26} -> ok, {size} bytes")
            print(f"     path      : {got}")
            print(f"     islink    : {os.path.islink(got)}  real_exists="
                  f"{os.path.exists(os.path.realpath(got))}")
        except Exception:
            print(f"  {fname:26} -> RAISED")
            traceback.print_exc()

    target = models.model_dir(name)
    print(f"model dir     : {target}")
    if target.is_dir():
        for f in sorted(target.rglob("*")):
            if f.is_file():
                print(f"   {f.name:26} link={f.is_symlink()} size={f.stat().st_size}")
    print(f"verify        : {models.verify(target)}")


def main() -> None:
    if "--diagnose" in sys.argv:
        i = sys.argv.index("--diagnose")
        diagnose(sys.argv[i + 1] if len(sys.argv) > i + 1 else "base")
        return
    ap = argparse.ArgumentParser(description="Media Toolkit")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true",
                    help="start the server only; do not open a window")
    ap.add_argument("--browser", action="store_true",
                    help="open in your normal browser instead of an app window")
    ap.add_argument("--server", action="store_true",
                    help="run headless and keep running after windows close")
    args = ap.parse_args()

    port = args.port or free_port()
    url = f"http://{args.host}:{port}"

    from app import config, shell

    log_path = config.DATA_ROOT / "app.log"
    if getattr(sys, "frozen", False):
        # No console in the packaged build: keep a log so failures are diagnosable.
        try:
            stream = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
            sys.stdout = sys.stderr = stream
            print(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} on {url} ---")
        except OSError:
            pass

    import uvicorn
    from app.main import app as fastapi_app, client_state
    from app import jobs as job_registry

    server = uvicorn.Server(uvicorn.Config(
        fastapi_app, host=args.host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not wait_for_server(port):
        shell.message_box("Media Toolkit",
                          "The app could not start.\n\nDetails were written to:\n"
                          f"{log_path}")
        return

    print(f"  Media Toolkit\n  {url}\n")

    if args.no_browser or args.server:
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return

    proc = None
    if args.browser:
        import webbrowser
        webbrowser.open(url)
    else:
        proc = shell.open_window(url, config.DATA_ROOT / "window")

    # Quit when the UI stops sending heartbeats. Until the very first one
    # arrives, keep waiting -- the window may still be starting up, and a slow
    # first paint must not be mistaken for a closed app.
    started_waiting = time.time()
    try:
        while thread.is_alive():
            time.sleep(0.5)
            # Never exit mid-job. A 3 GB model download or a live recording must
            # survive the window being closed.
            if job_registry.active_count() > 0:
                started_waiting = time.time()
                continue
            _, last_seen, seen_any = client_state()
            if not seen_any:
                # Give the window a generous window to appear; if the browser
                # died before ever loading, stop waiting.
                if proc is not None and proc.poll() is not None and \
                        time.time() - started_waiting > 20:
                    break
                if time.time() - started_waiting > 120:
                    break
                continue
            if time.time() - last_seen > GRACE_SECONDS:
                break
    except KeyboardInterrupt:
        pass

    server.should_exit = True
    thread.join(timeout=8)
    if proc and proc.poll() is None:
        proc.terminate()


if __name__ == "__main__":
    main()
