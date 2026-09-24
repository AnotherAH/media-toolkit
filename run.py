"""Launcher.

Starts the local server, opens a frameless Chromium app window against it, and
shuts everything down when that window closes, so it behaves like a desktop
app rather than a web page you have to remember to quit.

Closing the window is the quit signal, detected from the UI's heartbeats and
its event stream (a Chromium launch can hand off to an existing process and
exit at once, so the browser process itself proves nothing). Work in progress
outlives the window: downloads and recordings keep going in the background,
and launching the app again reattaches a window to the same running instance
instead of starting a second one that cannot see them.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# How long to keep running after heartbeats stop without a goodbye (a crashed
# or killed window), and after a goodbye with nothing following it (a closed
# window; a reload sends goodbye too, then its new page checks in).
GRACE_SECONDS = 12
GOODBYE_SECONDS = 5
PREFERRED_PORT = 8765
LOG_LIMIT = 2 * 1024 * 1024
BACKGROUND_NOTE = "Still working in the background. Open Media Toolkit again to see progress."


# -------------------------------------------------------------------- output

class _Log(io.TextIOBase):
    """app.log, rotated at 2 MB with one .1 backup, secrets removed on the
    way in. Optionally echoes to the console it replaced."""

    def __init__(self, path: Path, echo=None):
        self.path = path
        self.echo = echo
        self.lock = threading.Lock()
        self.fh = None
        try:
            from app.errors import redact
            self.redact = redact
        except Exception:
            self.redact = lambda text: text
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a", encoding="utf-8", errors="replace")
        try:
            self.size = self.path.stat().st_size
        except OSError:
            self.size = 0

    def _rotate(self) -> None:
        rotated = True
        try:
            self.fh.close()
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            rotated = False     # another launch has the file open; try again later
        self._open()
        if not rotated:
            self.size = 0       # ...after another LOG_LIMIT, not on every line

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        try:
            clean = self.redact(text)
        except Exception:
            clean = text
        with self.lock:
            try:
                if self.size + len(clean) > LOG_LIMIT:
                    self._rotate()
                self.fh.write(clean)
                self.fh.flush()
                self.size += len(clean.encode("utf-8", "replace"))
            except (OSError, ValueError):
                pass
        if self.echo is not None:
            try:
                self.echo.write(text)
                self.echo.flush()
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        try:
            self.fh.flush()
        except (OSError, ValueError):
            pass


def _setup_log(data_root: Path, console: bool) -> Path:
    path = data_root / "app.log"
    try:
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass                    # the running instance has it open; rotate next time
    try:
        echo = sys.stdout if (console and sys.stdout is not None) else None
        log = _Log(path, echo)
    except OSError:
        return path
    sys.stdout = sys.stderr = log
    return path


class _Parser(argparse.ArgumentParser):
    """argparse, but --help and usage errors still reach the user when the
    packaged exe has no console: they are shown in a message box."""

    def _print_message(self, message, file=None):
        if not message:
            return
        if sys.stdout is not None and not isinstance(sys.stdout, _Log):
            try:
                sys.stdout.write(message)
                sys.stdout.flush()
                return
            except Exception:
                pass
        from app import shell
        shell.message_box("Media Toolkit", message, info=True)


def _prepare_runtime() -> None:
    """Before anything imports yt_dlp or faster_whisper: put a user-updated
    yt-dlp ahead of the bundled one, and give faster_whisper an empty 'av'
    module when the real one is not shipped. Either module may be missing in
    an older checkout; then there is nothing to do."""
    try:
        from app import updater
        res = updater.activate()
        # The log is the only place that says whether a downloaded yt-dlp was
        # used, replaced by a newer bundled one, or set aside as broken.
        if isinstance(res, dict):
            used = f"downloaded {res.get('version')}" if res.get("active") else "bundled copy"
            print(f"yt-dlp: {used} ({res.get('reason') or 'ok'})")
    except ImportError:
        pass
    except Exception as exc:
        print(f"updater.activate failed: {exc}")
    try:
        from app import audio
        audio.install_av_stub()
    except ImportError:
        pass
    except Exception as exc:
        print(f"audio.install_av_stub failed: {exc}")


def _probe_hardware() -> None:
    """The first CUDA initialisation, here on the main thread before the
    server starts: on some driver states it crashed the process when a
    request thread did it (/api/setup on first page load). The result is
    cached, so no request has to probe again. app.main has already run
    config.bootstrap(), which puts the cuBLAS folders on PATH."""
    try:
        from app import hardware
        started = time.time()
        hardware.prime()
        print(f"hardware probed in {time.time() - started:.1f} s")
    except Exception as exc:
        print(f"hardware probe failed: {exc}")


# ------------------------------------------------------------------ diagnose

def diagnose(name: str, repair: bool, console: bool) -> int:
    """Report exactly what happens when this build fetches a speech model.

    Packaged builds can behave differently from a source checkout, so this
    runs the real download path inside the real environment and reports
    every step. It deletes the model first only with --repair. The report is
    written to DATA_ROOT/diagnose.txt and printed when there is a console,
    otherwise opened in the default text editor.
    """
    import traceback
    from app import config
    config.bootstrap()
    out = io.StringIO()

    def say(line: str = "") -> None:
        out.write(line + "\n")

    say(f"Media Toolkit diagnose, {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        from app import __version__
        say(f"version       : {__version__}")
    except ImportError:
        pass
    say(f"frozen        : {getattr(sys, 'frozen', False)}")
    say(f"python        : {sys.version.split()[0]}")
    say(f"data folder   : {config.DATA_ROOT}")
    try:
        from app import models
        known = set(getattr(models, "REPOS", {})) or {m["id"] for m in models.CATALOG}
        if name not in known:
            say(f"\nUnknown model '{name}'. Choose one of: {', '.join(sorted(known))}")
            return _finish_report(out.getvalue(), config.DATA_ROOT, console, 2)
        say(f"cache root    : {models.cache_root()}")
        try:
            import certifi
            say(f"certifi       : {certifi.where()} (exists={os.path.exists(certifi.where())})")
        except Exception as exc:
            say(f"certifi       : unavailable ({exc})")
        try:
            import hf_xet  # noqa: F401
            say("hf_xet        : importable")
        except Exception as exc:
            say(f"hf_xet        : NOT importable ({type(exc).__name__}: {exc})")
        say(f"repo          : {models.repo_id(name)}")
        target = models.model_dir(name)
        say(f"before        : {models.verify(target) if target.is_dir() else 'not downloaded'}")
        if repair:
            say("repair        : deleting the model and downloading it again")
            models.purge(name)
        started = time.time()
        last = [0.0]

        def progress(done: int, total: int) -> None:
            if time.time() - last[0] >= 5 or (total and done >= total):
                last[0] = time.time()
                say(f"  progress: {done / 1048576:,.0f} of {total / 1048576:,.0f} MB")

        try:
            path = models.ensure(name, on_progress=progress)
            say(f"ensure        : ok in {time.time() - started:.1f} s -> {path}")
        except Exception:
            say("ensure        : RAISED")
            say(traceback.format_exc())
        target = models.model_dir(name)
        say(f"model dir     : {target}")
        if target.is_dir():
            for f in sorted(target.rglob("*")):
                if f.is_file():
                    say(f"   {f.name:26} link={f.is_symlink()} size={f.stat().st_size}")
        say(f"verify        : {models.verify(target) if target.is_dir() else 'missing'}")
    except Exception:
        say("diagnose itself failed:")
        say(traceback.format_exc())
    return _finish_report(out.getvalue(), config.DATA_ROOT, console, 0)


def _finish_report(text: str, data_root: Path, console: bool, code: int) -> int:
    report = data_root / "diagnose.txt"
    try:
        report.write_text(text, encoding="utf-8")
    except OSError:
        report = None
    if console:
        print(text)
        if report:
            print(f"(also saved to {report})")
    elif report and os.name == "nt":
        os.startfile(str(report))                           # noqa: S606
    return code


# ------------------------------------------------------------ single instance

class InstanceLock:
    """Held for the whole life of the running instance. A second launch that
    cannot take it knows an instance owns this data folder."""

    def __init__(self, data_root: Path):
        self.path = data_root / "instance.lock"
        self.fh = None

    def acquire(self) -> bool:
        try:
            self.fh = open(self.path, "a+b")
            self.fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.fh:
                self.fh.close()
                self.fh = None
            return False

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def _instance_file(data_root: Path) -> Path:
    return data_root / "instance.json"


def _ask_instance(info: dict, timeout: float = 2.0) -> bool:
    """Is the server in instance.json really ours and alive?"""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://{_connect_host(info)}:{int(info['port'])}/api/instance",
                                     headers={"X-MT-Token": str(info["token"])})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return bool(data.get("ok")) and int(data.get("pid", -1)) == int(info.get("pid", -2))
    except Exception:
        return False


def hold_instance(info: dict) -> bool:
    """Tell a running instance a window is on its way. An instance that is
    only running for work left behind quits once that work ends, and the
    window this launch opens needs a few seconds to check in; one heartbeat
    gives it the usual grace period instead of a page on a closed port."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"http://{_connect_host(info)}:{int(info['port'])}/api/heartbeat", data=b"",
            method="POST", headers={"X-MT-Token": str(info["token"])})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


def find_running(data_root: Path, wait: float = 0.0) -> dict | None:
    """instance.json of a live instance for this data folder, waiting up to
    ``wait`` seconds for one that is still starting."""
    deadline = time.time() + wait
    while True:
        try:
            info = json.loads(_instance_file(data_root).read_text("utf-8"))
            if _ask_instance(info):
                return info
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.4)


def _connect_host(info: dict) -> str:
    """Where a second launch reaches the running instance: loopback, unless
    that instance listens on one specific network address only."""
    host = str(info.get("host") or "127.0.0.1")
    if host in ("::1", "::"):                 # an IPv6 socket takes no IPv4 calls
        return "[::1]"
    if host in _LOOPBACK or host == "0.0.0.0":
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


def _write_instance(data_root: Path, port: int, token: str, remote: bool = False,
                    host: str = "127.0.0.1") -> None:
    path = _instance_file(data_root)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"port": port, "pid": os.getpid(), "token": token,
                                   "remote": remote, "host": host, "started": time.time()}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"could not write {path}: {exc}")


def _remove_instance(data_root: Path) -> None:
    path = _instance_file(data_root)
    try:
        if json.loads(path.read_text("utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------- networking

_LOOPBACK = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket here, before uvicorn, so a busy port is an
    error we can report instead of a window opened on some other program.
    Port 0 means: the usual port if free, else the next few, else any."""
    addr = _LOOPBACK.get(host, host)
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET

    def try_bind(p: int) -> socket.socket | None:
        s = socket.socket(family, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind((addr, p))
            s.listen(128)
            s.set_inheritable(False)
            return s
        except OSError:
            s.close()
            return None

    if port:
        sock = try_bind(port)
        if sock is None:
            raise OSError(f"Port {port} is already in use by another program.")
        return sock
    # A stable port keeps the app window's saved preferences (they belong to
    # the address) from one launch to the next.
    for p in range(PREFERRED_PORT, PREFERRED_PORT + 40):
        sock = try_bind(p)
        if sock is not None:
            return sock
    sock = try_bind(0)
    if sock is None:
        raise OSError("No free port to listen on.")
    return sock


def wait_started(server, thread: threading.Thread, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(server, "started", False):
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.05)
    return False


def _lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))           # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


# --------------------------------------------------------------------- main

def open_ui(url: str, use_browser: bool, data_root: Path) -> object:
    from app import config, shell
    if use_browser:
        import webbrowser
        webbrowser.open(url)
        return None
    return shell.open_window(url, data_root / "window", proxy=config.get().get("proxy", ""))


def window_gone(state: dict, proc, started: float, now: float | None = None) -> bool:
    """Has the app window gone away? ``state`` is app.main.liveness()."""
    now = now or time.time()
    if not state["seen_any"]:
        # Give the window a generous time to appear; if the browser died
        # before ever loading the page, stop waiting sooner.
        if proc is not None and proc.poll() is not None and now - started > 20:
            return True
        return now - started > 120
    goodbye = state.get("goodbye_at") or 0.0
    if goodbye and state["clients"] == 0 and state["last_beat"] < goodbye \
            and now - goodbye > GOODBYE_SECONDS:
        return True
    if state["clients"] > 0:
        return False
    return now - max(state["last_seen"], state["last_beat"]) > GRACE_SECONDS


def main(argv: list[str] | None = None) -> int:
    from app import shell
    console = shell.attach_console()

    ap = _Parser(prog="MediaToolkit", description="Media Toolkit: download, record and "
                 "transcribe videos on this PC.")
    ap.add_argument("--port", type=int, default=0,
                    help="port to listen on (default: 8765, or the next free one)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on. Anything but 127.0.0.1 makes the app reachable "
                         "from other computers; an access key is then required and printed")
    ap.add_argument("--server", action="store_true",
                    help="run without a window and keep running until stopped (Ctrl+C)")
    ap.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # old name
    ap.add_argument("--browser", action="store_true",
                    help="open in your normal browser instead of an app window")
    ap.add_argument("--diagnose", metavar="MODEL", nargs="?", const="base",
                    help="report on downloading a speech model (default: base)")
    ap.add_argument("--repair", action="store_true",
                    help="with --diagnose: delete the model first and download it again")
    try:
        from app import __version__
    except ImportError:
        __version__ = "?"
    ap.add_argument("--version", action="version", version=f"Media Toolkit {__version__}")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    from app import config
    log_path = _setup_log(config.DATA_ROOT, console)
    print(f"\n--- Media Toolkit {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    if args.diagnose is not None:
        _prepare_runtime()
        return diagnose(args.diagnose, args.repair, console)

    headless = args.server or args.no_browser
    loopback = args.host in _LOOPBACK

    # The runtime is prepared only once this launch owns the data folder: a
    # second launch that just hands over to the running instance never needs
    # yt-dlp, and must not wait on (or touch) the downloaded copy it uses.
    lock = InstanceLock(config.DATA_ROOT)
    if not lock.acquire():
        running = find_running(config.DATA_ROOT, wait=30)
        if not running:
            shell.message_box("Media Toolkit", "Media Toolkit is already starting, or did not "
                                               "shut down cleanly. Try again in a moment.")
            return 1
        url = f"http://{_connect_host(running)}:{running['port']}"
        if running.get("remote"):            # that instance wants its key on every request
            url += f"/?token={running['token']}"
        if headless:
            print(f"Media Toolkit is already running at {url}")
        else:
            hold_instance(running)
            open_ui(url, args.browser, config.DATA_ROOT)
        return 0

    try:
        _prepare_runtime()
        return _serve(args, headless, loopback, log_path, console)
    finally:
        _remove_instance(config.DATA_ROOT)
        lock.release()


def _serve(args, headless: bool, loopback: bool, log_path: Path, console: bool) -> int:
    from app import config, shell
    try:
        sock = bind_socket(args.host, args.port)
    except OSError as exc:
        shell.message_box("Media Toolkit", f"Media Toolkit could not start.\n\n{exc}")
        return 1
    port = sock.getsockname()[1]

    import uvicorn
    from app import jobs
    from app import main as appmain

    appmain.configure(remote=not loopback)
    _probe_hardware()
    if not loopback:
        print("WARNING: listening on a network address. Anyone who can reach this computer "
              "and has the access key below can use Media Toolkit, including your sign-ins.")

    server = uvicorn.Server(uvicorn.Config(appmain.app, host=args.host, port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              name="server", daemon=True)
    thread.start()
    if not wait_started(server, thread):
        shell.message_box("Media Toolkit", "The app could not start.\n\nDetails were written to:\n"
                                           f"{log_path}")
        return 1
    _write_instance(config.DATA_ROOT, port, appmain.TOKEN, remote=not loopback, host=args.host)

    local_url = f"http://127.0.0.1:{port}"
    if loopback:
        shown = local_url
    else:
        host = _lan_address() if args.host in ("0.0.0.0", "::") else args.host
        shown = f"http://{host}:{port}/?token={appmain.TOKEN}"
    print(f"  Media Toolkit\n  {shown}\n")

    try:
        if headless:
            if not console and getattr(sys, "frozen", False):
                shell.notify("Media Toolkit", f"Media Toolkit is running at\n{shown}\n\n"
                                              "It keeps running until you end it in Task Manager.")
            while thread.is_alive():
                time.sleep(0.5)
        else:
            _run_windowed(args, thread, shown if not loopback else local_url)
    except KeyboardInterrupt:
        stopping = jobs.cancel_all()
        if stopping:
            print(f"Stopping {stopping} job(s)...")
            deadline = time.time() + 25
            while jobs.active_count() and time.time() < deadline:
                time.sleep(0.25)

    server.should_exit = True
    thread.join(timeout=8)
    return 0


def _run_windowed(args, thread: threading.Thread, url: str) -> None:
    from app import config, jobs, shell
    from app import main as appmain

    proc = open_ui(url, args.browser, config.DATA_ROOT)
    started = time.time()
    told = False
    while thread.is_alive():
        time.sleep(0.5)
        if not window_gone(appmain.liveness(), proc, started):
            continue
        # Never quit mid-job: a model download, a download or a recording that
        # is still being saved must survive the window being closed.
        if jobs.active_count() > 0:
            if not told:
                told = True
                shell.notify("Media Toolkit", BACKGROUND_NOTE)
            continue
        break
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
