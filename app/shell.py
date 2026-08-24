"""Native-feeling app window.

Chromium's `--app=` mode gives a frameless window with no tabs, no address bar
and no bookmarks -- it reads as a desktop app rather than a web page. It runs
against a private profile directory, so it never touches the browser the user
actually browses with: no shared cookies, no session, no history, and closing it
does not disturb their real windows.

Falls back to the default browser when no Chromium-family browser exists.
"""
from __future__ import annotations

import os
import subprocess
import sys
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


def open_window(url: str, profile_dir: Path) -> subprocess.Popen | None:
    """Open the app window. Returns the browser process, or None if we fell
    back to the default browser."""
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
    try:
        return subprocess.Popen(
            args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except Exception:
        webbrowser.open(url)
        return None


def message_box(title: str, text: str) -> None:
    """Report a startup failure when there is no console to print to."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr)
