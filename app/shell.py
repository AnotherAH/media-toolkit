"""Native-feeling app window, message boxes, and child-process hygiene.

Chromium's `--app=` mode gives a frameless window with no tabs, no address bar
and no bookmarks, so it reads as a desktop app rather than a web page. It runs
against a private profile directory, so it never touches the browser the user
actually browses with: no shared cookies, no session, no history, and closing
it does not disturb their real windows.

Falls back to the default browser when no Chromium-family browser exists.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

WINDOW = ("--window-size=1360,900", "--window-position=120,60")


def find_browser() -> tuple[str, Path] | None:
    if os.name == "nt":
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        candidates = [
            # Edge first: present on every Windows 10/11 machine.
            ("Edge", pf86 / "Microsoft/Edge/Application/msedge.exe"),
            ("Edge", pf / "Microsoft/Edge/Application/msedge.exe"),
            ("Chrome", pf / "Google/Chrome/Application/chrome.exe"),
            ("Chrome", pf86 / "Google/Chrome/Application/chrome.exe"),
            ("Chrome", local / "Google/Chrome/Application/chrome.exe"),
            ("Brave", pf / "BraveSoftware/Brave-Browser/Application/brave.exe"),
            ("Vivaldi", local / "Vivaldi/Application/vivaldi.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            ("Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
            ("Edge", Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
            ("Brave", Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")),
        ]
    else:
        from shutil import which
        candidates = [(n, Path(p)) for n, p in (
            ("Chrome", which("google-chrome") or ""),
            ("Chromium", which("chromium") or which("chromium-browser") or ""),
            ("Edge", which("microsoft-edge") or ""),
            ("Brave", which("brave-browser") or "")) if p]

    for name, path in candidates:
        if path and path.exists():
            return name, path
    return None


def open_window(url: str, profile_dir: Path, proxy: str = "") -> subprocess.Popen | None:
    """Open the app window. Returns the browser process, or None if we fell
    back to the default browser.

    With a proxy set, thumbnails and previews go through it too, like the
    downloads do; Chromium never proxies loopback, so the app still reaches
    its own server directly.
    """
    found = find_browser()
    if not found:
        webbrowser.open(url)
        return None
    _, exe = found
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        *WINDOW,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--no-service-autorun",
        "--disable-breakpad",
    ]
    if proxy and re.match(r"^(https?|socks4a?|socks5h?)://[^\s\"']+$", proxy, re.I):
        # Chromium takes host:port with a scheme but no credentials.
        args.append(f"--proxy-server={re.sub(r'//[^/@]*@', '//', proxy)}")
    try:
        return subprocess.Popen(
            args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except Exception:
        webbrowser.open(url)
        return None


_MB_ICONERROR = 0x10
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000


def message_box(title: str, text: str, info: bool = False) -> None:
    """Tell the user something when there is no console to print to. Blocks
    until dismissed."""
    if os.name == "nt":
        try:
            import ctypes
            flags = (_MB_ICONINFORMATION if info else _MB_ICONERROR) | _MB_SETFOREGROUND | _MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr)


def notify(title: str, text: str) -> threading.Thread:
    """A message box that does not stop the caller (the launcher keeps
    watching its jobs while the box is up)."""
    t = threading.Thread(target=message_box, args=(title, text, True),
                         name="message-box", daemon=True)
    t.start()
    return t


# ------------------------------------------------- children die with the app

_job_handle = None
_job_lock = threading.Lock()


def _kill_on_close_job():
    """One Windows job object per process, created on first use. Processes
    assigned to it are killed when this process ends, however it ends
    (including End task in Task Manager)."""
    global _job_handle
    with _job_lock:
        if _job_handle is not None:
            return _job_handle or None
        _job_handle = 0
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return None

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000     # KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info),
                                               ctypes.sizeof(info)):
                k32.CloseHandle(wintypes.HANDLE(job))
                return None
            _job_handle = job
            return job
        except Exception:
            return None


def kill_with_app(proc: subprocess.Popen) -> bool:
    """Make a helper process (ffmpeg recording a stream) end when the app
    ends, even if the app is killed. Only for helpers: never for anything the
    user opened, such as a video player. Returns False where unsupported."""
    job = _kill_on_close_job()
    if not job or proc is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return False
        return bool(ctypes.windll.kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(int(handle))))
    except Exception:
        return False


# ----------------------------------------------- console for the windowed exe

def attach_console() -> bool:
    """The packaged exe has no console of its own. When it was started from
    a terminal, write to that terminal instead, so --help, --version,
    --diagnose and --server output is visible. Returns True when attached."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return sys.stdout is not None
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):          # ATTACH_PARENT_PROCESS
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = sys.stdout
        return True
    except Exception:
        return False
