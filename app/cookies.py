"""Browser sign-ins, so nobody has to hand-make a cookies.txt.

Sites check sessions on their own servers, so the only sign-in that works is
a real one. What this module removes is the manual work: it finds the
browsers installed on the machine, actually tries to read cookies out of each
one, and reports which ones work, so "use my account" is one click instead of
a browser-extension hunt.

Windows note: since Chrome 127, Chromium browsers seal their cookie store with
app-bound encryption, so Chrome/Edge/Brave frequently fail while Firefox works.
Rather than guessing, we test and report the truth.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

BROWSERS = [
    {"id": "firefox", "label": "Firefox", "family": "gecko"},
    {"id": "librewolf", "label": "LibreWolf", "family": "gecko"},
    {"id": "chrome", "label": "Chrome", "family": "chromium"},
    {"id": "edge", "label": "Edge", "family": "chromium"},
    {"id": "brave", "label": "Brave", "family": "chromium"},
    {"id": "opera", "label": "Opera", "family": "chromium"},
    {"id": "vivaldi", "label": "Vivaldi", "family": "chromium"},
    {"id": "chromium", "label": "Chromium", "family": "chromium"},
    {"id": "whale", "label": "Whale", "family": "chromium"},
]

# Where each browser keeps its profile, per platform. Used only to decide
# whether it is worth *trying* a browser at all.
def _roots() -> dict[str, list[Path]]:
    home = Path.home()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        return {
            "firefox": [roaming / "Mozilla/Firefox"],
            "librewolf": [roaming / "librewolf"],
            "chrome": [local / "Google/Chrome/User Data"],
            "edge": [local / "Microsoft/Edge/User Data"],
            "brave": [local / "BraveSoftware/Brave-Browser/User Data"],
            "opera": [roaming / "Opera Software/Opera Stable"],
            "vivaldi": [local / "Vivaldi/User Data"],
            "chromium": [local / "Chromium/User Data"],
            "whale": [local / "Naver/Naver Whale/User Data"],
        }
    if sys.platform == "darwin":
        s = home / "Library/Application Support"
        return {
            "firefox": [s / "Firefox"], "librewolf": [s / "librewolf"],
            "chrome": [s / "Google/Chrome"], "edge": [s / "Microsoft Edge"],
            "brave": [s / "BraveSoftware/Brave-Browser"], "opera": [s / "com.operasoftware.Opera"],
            "vivaldi": [s / "Vivaldi"], "chromium": [s / "Chromium"], "whale": [s / "Naver/Whale"],
        }
    cfg = home / ".config"
    return {
        "firefox": [home / ".mozilla/firefox", home / "snap/firefox/common/.mozilla/firefox"],
        "librewolf": [home / ".librewolf"],
        "chrome": [cfg / "google-chrome"], "edge": [cfg / "microsoft-edge"],
        "brave": [cfg / "BraveSoftware/Brave-Browser"], "opera": [cfg / "opera"],
        "vivaldi": [cfg / "vivaldi"], "chromium": [cfg / "chromium"], "whale": [cfg / "naver-whale"],
    }


def installed() -> list[dict]:
    roots = _roots()
    out = []
    for b in BROWSERS:
        paths = roots.get(b["id"], [])
        hit = next((p for p in paths if p.exists()), None)
        if hit:
            out.append({**b, "path": str(hit)})
    return out


def _running(browser_id: str) -> bool:
    """Chromium locks its cookie DB while running; worth telling the user."""
    if os.name != "nt":
        return False
    names = {"chrome": "chrome.exe", "edge": "msedge.exe", "brave": "brave.exe",
             "opera": "opera.exe", "vivaldi": "vivaldi.exe", "chromium": "chrome.exe",
             "firefox": "firefox.exe", "librewolf": "librewolf.exe", "whale": "whale.exe"}
    exe = names.get(browser_id)
    if not exe:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=12,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return exe.lower() in out.stdout.lower()
    except Exception:
        return False


class _Collector:
    """yt-dlp reports cookie failures through its logger, not the exception, so
    capture them to explain *why* a browser refused."""

    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.errors.append(str(msg))

    def error(self, msg):
        self.errors.append(str(msg))


def test_browser(browser_id: str, profile: str = "") -> dict:
    """Actually pull the cookie jar. Returns what really happened, not a guess."""
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import load_cookies

    spec = (browser_id, profile or None, None, None)
    log = _Collector()
    result = {"browser": browser_id, "profile": profile, "ok": False,
              "count": 0, "domains": [], "error": "", "running": _running(browser_id)}
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "logger": log}) as ydl:
            jar = load_cookies(None, spec, ydl)
        result["count"] = len(jar)
        interesting = {"instagram.com", "youtube.com", "google.com", "tiktok.com",
                       "x.com", "twitter.com", "facebook.com", "reddit.com",
                       "vimeo.com", "patreon.com", "twitch.tv", "soundcloud.com"}
        seen = set()
        for c in jar:
            dom = (c.domain or "").lstrip(".")
            for known in interesting:
                if dom == known or dom.endswith("." + known):
                    seen.add(known)
        result["domains"] = sorted(seen)
        result["ok"] = result["count"] > 0
        if not result["ok"]:
            result["error"] = "Couldn't read any sign-ins from this browser."
    except Exception as exc:
        result["error"] = _diagnose(f"{exc} {' '.join(log.errors)}", browser_id)
    if not result["ok"] and log.errors and not result["error"]:
        result["error"] = _diagnose(" ".join(log.errors), browser_id)
    return result


def _diagnose(text: str, browser_id: str) -> str:
    label = next((b["label"] for b in BROWSERS if b["id"] == browser_id), "the browser")
    low = text.lower()
    if "could not copy" in low or "being used" in low or "permission denied" in low:
        return f"Couldn't read it. Close {label} completely and try again."
    if "dpapi" in low or "decrypt" in low or "v20" in low or "app-bound" in low:
        return (f"{label} keeps its sign-ins locked to itself on Windows. Use "
                "\u201cSign in with a new window\u201d instead, or Firefox.")
    if "unsupported" in low or "could not find" in low or "no such" in low or "not found" in low:
        return "No usable profile found for this browser."
    return (text.strip()[:180] or "Couldn't read it.")


# ------------------------------------------------- built-in login browser
# The reliable route on Windows: instead of trying to decrypt a browser's
# sealed cookie store, we start a Chromium browser ourselves against a profile
# folder we own, with its DevTools endpoint enabled. The user signs in
# normally in that window, and we ask the running browser for its own
# cookies. No extension, no cookies.txt, no decryption.
#
# The DevTools port is chosen by the browser (port 0) and read back from the
# profile's DevToolsActivePort file, so it is never a fixed, guessable port,
# and the browser is closed as soon as the cookies are collected.

LOGIN_PROFILE = "login-profile"


def _chromium_binaries() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    if os.name == "nt":
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        candidates = [
            ("Edge", pf86 / "Microsoft/Edge/Application/msedge.exe"),
            ("Edge", pf / "Microsoft/Edge/Application/msedge.exe"),
            ("Chrome", pf / "Google/Chrome/Application/chrome.exe"),
            ("Chrome", pf86 / "Google/Chrome/Application/chrome.exe"),
            ("Chrome", local / "Google/Chrome/Application/chrome.exe"),
            ("Brave", pf / "BraveSoftware/Brave-Browser/Application/brave.exe"),
            ("Brave", pf86 / "BraveSoftware/Brave-Browser/Application/brave.exe"),
            ("Vivaldi", local / "Vivaldi/Application/vivaldi.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            ("Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
            ("Edge", Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
            ("Brave", Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")),
        ]
    else:
        candidates = [(n, Path(p)) for n, p in
                      (("Chrome", shutil.which("google-chrome") or ""),
                       ("Chromium", shutil.which("chromium") or ""),
                       ("Edge", shutil.which("microsoft-edge") or ""),
                       ("Brave", shutil.which("brave-browser") or "")) if p]
    for name, path in candidates:
        if path and path.exists() and not any(p == path for _, p in out):
            out.append((name, path))
    return out


def login_profile_dir(root: Path) -> Path:
    d = root / LOGIN_PROFILE
    d.mkdir(parents=True, exist_ok=True)
    return d


def _devtools_endpoint(profile: Path, timeout: float = 2.0) -> str | None:
    """The running sign-in browser's DevTools websocket URL, or None."""
    import urllib.request
    try:
        lines = (profile / "DevToolsActivePort").read_text("utf-8").splitlines()
        port = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return json.loads(r.read())["webSocketDebuggerUrl"]
    except Exception:
        return None


def _cdp(ws_url: str, method: str, timeout: float = 10.0) -> dict:
    from websockets.sync.client import connect
    with connect(ws_url, open_timeout=timeout, close_timeout=timeout,
                 max_size=64 * 1024 * 1024) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": {}}))
        while True:
            msg = json.loads(ws.recv(timeout=timeout))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "DevTools refused"))
                return msg.get("result", {})


def start_login_browser(root: Path, url: str = "https://www.instagram.com/accounts/login/") -> dict:
    """Open a browser we control, pointed at the site's login page."""
    url = (url or "").strip()
    # The URL goes on the browser's command line: anything but a plain web
    # address could be read as a browser switch.
    if not re.match(r"^https?://[^\s\"']+$", url, re.I):
        return {"ok": False, "error": "Use a web address that starts with https://"}
    bins = _chromium_binaries()
    if not bins:
        return {"ok": False, "error": "No Chrome, Edge, Brave or Vivaldi found. Install one, or "
                                      "use Firefox with \u201cUse my browser's sign-ins\u201d."}
    name, exe = bins[0]
    profile = login_profile_dir(root)
    if _devtools_endpoint(profile) is None:
        (profile / "DevToolsActivePort").unlink(missing_ok=True)      # stale
    args = [
        str(exe),
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--new-window",
        "--",
        url,
    ]
    try:
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         if os.name == "nt" else 0)
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't start {name}: {exc}"}
    return {"ok": True, "browser": name, "profile": str(profile),
            "message": f"{name} opened. Sign in there, then come back and press Done."}


def _close_login_browser(profile: Path) -> bool:
    ws = _devtools_endpoint(profile)
    if not ws:
        return False
    try:
        _cdp(ws, "Browser.close", timeout=5)
    except Exception:
        pass            # the socket drops as the browser closes; that is success
    for _ in range(20):
        if _devtools_endpoint(profile, timeout=0.5) is None:
            return True
        time.sleep(0.25)
    return True


# ------------------------------------------------------ cookies.txt files

_HEADER = "# Netscape HTTP Cookie File"


def _read_jar(path: Path) -> list[list[str]]:
    """Cookie rows of an existing cookies.txt (7 tab-separated fields)."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return []
    return _rows(text)


def _row(domain: str, path: str, secure: bool, expires, name: str, value: str) -> list[str] | None:
    """One cookies.txt row, or None when the cookie cannot be written safely.

    Python's cookie loader, which yt-dlp uses, rejects the WHOLE file when a
    row's "include subdomains" flag disagrees with a leading dot on the
    domain, so the flag is always derived from the domain here. A tab or line
    break inside a value would split the row and is refused, and a name must
    look like one (a pasted JSON export is not a cookie header).
    """
    host = domain[len("#HttpOnly_"):] if domain.startswith("#HttpOnly_") else domain
    if not host or not _NAME.fullmatch(name or "") or re.search(r"[\t\r\n\x00]", value or ""):
        return None
    try:
        stamp = max(int(float(expires or 0)), 0)
    except (TypeError, ValueError):
        return None
    return [domain, "TRUE" if host.startswith(".") else "FALSE", path or "/",
            "TRUE" if secure else "FALSE", str(stamp), name, value or ""]


# A cookie name as sites really use them: anything but spaces, separators and
# the brackets and quotes of a pasted JSON export.
_NAME = re.compile(r'[^\s=;,"{}\[\]\\\x00-\x1f\x7f]+')


def _rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _, path, secure, expires, name, value = parts
        row = _row(domain.strip(), path, secure.strip().upper() == "TRUE",
                   expires.strip(), name.strip(), value)
        if row:
            rows.append(row)
    return rows


def _key(row: list[str]) -> tuple[str, str, str]:
    domain = row[0][len("#HttpOnly_"):] if row[0].startswith("#HttpOnly_") else row[0]
    return domain.lower(), row[2], row[5]


def _write_jar(dest: Path, rows: list[list[str]], merge: bool = True) -> int:
    """Merge ``rows`` into dest (same domain, path and name replaces), written
    atomically: yt-dlp may be reading the file from another job right now."""
    merged: dict[tuple, list[str]] = {}
    if merge:
        for row in _read_jar(dest):
            merged[_key(row)] = row
    for row in rows:
        merged[_key(row)] = row
    lines = [_HEADER, "# Written by Media Toolkit. Contains sign-ins; keep it private."]
    lines += ["\t".join(r) for r in merged.values()]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return len(merged)


# Per-run copies of the user's cookies.txt, one folder per process so a
# second launch never deletes copies the first one is using.
_COPIES = Path(tempfile.gettempdir()) / "media-toolkit" / "cookies" / str(os.getpid())


def private_copy(src: str | Path) -> str:
    """A throwaway copy of a cookies.txt for one yt-dlp instance.

    yt-dlp rewrites its cookie file when it closes, in place. With a link
    preview, a download and a recording running at once, one instance read
    another's half-written file (NUL bytes, "invalid Netscape format") or
    wrote a partial jar back, and the saved sign-in was lost for good. Give
    each instance its own copy: the user's file is then only ever written by
    Media Toolkit, atomically. Copies are deleted when the app exits, and the
    startup clean-up removes any a crash left behind.
    """
    _COPIES.mkdir(parents=True, exist_ok=True)
    dest = _COPIES / f"{uuid.uuid4().hex}.txt"
    shutil.copyfile(src, dest)
    return str(dest)


def discard_copy(path: str | Path | None) -> None:
    """Delete a copy made by private_copy (anything else is left alone)."""
    if not path:
        return
    p = Path(path)
    if p.parent == _COPIES:
        try:
            p.unlink()
        except OSError:
            pass


@atexit.register
def _remove_copies() -> None:
    shutil.rmtree(_COPIES, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)                 # signal 0 only checks, on POSIX
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                        # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def remove_stale_copies() -> int:
    """Delete cookie copies left by a Media Toolkit that crashed or was
    killed: they hold sign-ins and must not linger in the temp folder."""
    removed = 0
    root = _COPIES.parent
    if not root.is_dir():
        return 0
    for folder in root.iterdir():
        if not folder.is_dir() or not folder.name.isdigit() or folder == _COPIES:
            continue
        try:
            alive = _pid_alive(int(folder.name))
        except Exception:
            alive = True
        if not alive:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def harvest_login_cookies(dest: Path, profile: Path | None = None) -> dict:
    """Save the sign-in window's cookies into dest, then close that window."""
    if profile is None:
        profile = dest.parent / LOGIN_PROFILE
    ws = _devtools_endpoint(profile)
    if not ws:
        return {"ok": False, "error": "Couldn't reach the sign-in window. Make sure it's still "
                                      "open, then press Done again."}
    try:
        raw = _cdp(ws, "Storage.getCookies").get("cookies", [])
    except Exception as exc:
        return {"ok": False, "error": "Couldn't reach the sign-in window. Make sure it's still "
                                      f"open, then press Done again. ({str(exc)[:120]})"}
    if not raw:
        return {"ok": False, "error": "That window has no sign-ins yet. Sign in first, then "
                                      "press Done."}

    rows = []
    for c in raw:
        # expires -1 (a session cookie) is written as 0, so it stays one.
        row = _row(str(c.get("domain") or ""), str(c.get("path") or "/"), bool(c.get("secure")),
                   c.get("expires"), str(c.get("name") or ""), str(c.get("value") or ""))
        if row:
            rows.append(row)
    if not rows:
        return {"ok": False, "error": "That window has no sign-ins yet. Sign in first, then "
                                      "press Done."}
    _write_jar(dest, rows)
    _close_login_browser(profile)

    sites = sorted({d.lstrip(".") for d in (c.get("domain", "") for c in raw) if d})
    notable = [s for s in sites if any(k in s for k in
               ("instagram", "youtube", "tiktok", "x.com", "twitter", "facebook",
                "reddit", "vimeo", "patreon", "twitch"))]
    return {"ok": True, "path": str(dest), "cookies": len(rows),
            "sites": notable[:12], "total_domains": len(sites)}


def forget(root: Path, jar: Path) -> dict:
    """Sign out everywhere Media Toolkit stored a sign-in: close the sign-in
    window, delete its browser profile (it stays signed in otherwise) and the
    saved cookies file."""
    removed: list[str] = []
    profile = root / LOGIN_PROFILE
    _close_login_browser(profile)
    try:
        if jar.exists():
            jar.unlink()
            removed.append("saved cookies")
    except OSError:
        pass
    if profile.exists():
        for _ in range(12):                    # the browser may take a moment to let go
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)
        if not profile.exists():
            removed.append("the sign-in window's browser profile")
    return {"removed": removed, "profile_left": profile.exists()}


def autodetect() -> dict:
    """Try every installed browser and rank the ones that actually work."""
    found = installed()
    results = [test_browser(b["id"]) for b in found]
    for r, b in zip(results, found):
        r["label"] = b["label"]
    working = [r for r in results if r["ok"]]
    # Prefer the jar that already carries sessions for sites people download from,
    # then simply the biggest jar.
    working.sort(key=lambda r: (len(r["domains"]), r["count"]), reverse=True)
    return {
        "tested": results,
        "working": working,
        "best": working[0]["browser"] if working else "",
        "advice": _advice(results, working),
    }


def _advice(results: list[dict], working: list[dict]) -> str:
    if working:
        best = working[0]
        where = f" · signed in to {', '.join(best['domains'][:4])}" if best["domains"] else ""
        return f"Using {best.get('label', best['browser'])}{where}."
    locked = [r for r in results if "close" in r["error"].lower()]
    if locked:
        names = ", ".join(r.get("label", r["browser"]) for r in locked)
        return f"Close {names} completely and try again. Sign-ins can't be read while it's open."
    if results:
        return ("None of your browsers would share their sign-ins. Chrome and Edge lock them "
                "on Windows; use \u201cSign in with a new window\u201d instead, or Firefox.")
    return "No browsers found. Paste cookies under Advanced instead."


# Names people type for the sites they sign in to most.
_SITE_ALIASES = {"youtube": "youtube.com", "youtu.be": "youtube.com", "instagram": "instagram.com",
                 "tiktok": "tiktok.com", "x": "x.com", "twitter": "x.com", "twitter.com": "x.com",
                 "facebook": "facebook.com", "fb.watch": "facebook.com", "reddit": "reddit.com",
                 "vimeo": "vimeo.com", "twitch": "twitch.tv", "patreon": "patreon.com",
                 "soundcloud": "soundcloud.com", "kick": "kick.com"}
_TWO_LEVEL = {"co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au",
              "co.jp", "ne.jp", "or.jp", "com.br", "com.tr", "co.in", "co.kr", "com.mx",
              "com.cn", "com.tw", "co.nz", "co.za", "com.ar", "com.sg", "com.hk", "co.id"}


def site_domain(site: str) -> str:
    """The cookie domain for what the user typed: 'YouTube',
    'https://www.instagram.com/p/x', 'm.tiktok.com' -> '.youtube.com' ..."""
    s = (site or "").strip().lower()
    if not s:
        raise ValueError("Choose which site these cookies are for.")
    if s in _SITE_ALIASES:
        return "." + _SITE_ALIASES[s]
    try:
        host = (urlsplit(s if "://" in s else "https://" + s).hostname or "").strip(".")
    except ValueError:
        host = ""
    host = _SITE_ALIASES.get(host, host)
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("That doesn't look like a website. Enter an address such as youtube.com.")
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL else 2
    base = ".".join(labels[-keep:])
    return "." + _SITE_ALIASES.get(base, base)


def import_text(raw: str, dest: Path, site: str = "") -> dict:
    """Accept a pasted cookies.txt, or a raw `name=value; name=value` header
    for the site the user names, and merge it into our cookie file so the
    user never edits one by hand.

    A header carries no domain. Guessing one sent Google and X session tokens
    to Instagram, so the site is required and nothing is ever guessed.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Nothing pasted.")

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    looks_netscape = any(ln.startswith("#") for ln in lines[:3]) or \
        any(len(ln.split("\t")) >= 7 for ln in lines)
    if looks_netscape:
        rows = _rows(raw)
        if not rows:
            raise ValueError("Couldn't find any cookies in that. Paste a whole cookies.txt, "
                             "or a \u201cname=value; name=value\u201d header.")
        _write_jar(dest, rows)
        domains = sorted({_key(r)[0].lstrip(".") for r in rows})
        return {"path": str(dest), "cookies": len(rows), "format": "netscape",
                "domains": domains[:12]}

    # Header form: "sessionid=abc; csrftoken=def"
    pairs = [p.strip() for p in raw.replace("\n", ";").split(";") if "=" in p]
    if not pairs:
        raise ValueError("Couldn't recognise that as cookies. Paste a whole cookies.txt, "
                         "or a \u201cname=value; name=value\u201d header.")
    domain = site_domain(site)
    rows = []
    for pair in pairs:
        name, _, value = pair.partition("=")
        name = name.strip()
        if name.lower().startswith("cookie:"):
            name = name[len("cookie:"):].strip()
        row = _row(domain, "/", True, 0, name, value.strip())
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("Couldn't recognise that as cookies. Paste a whole cookies.txt, "
                         "or a \u201cname=value; name=value\u201d header.")
    _write_jar(dest, rows)
    return {"path": str(dest), "cookies": len(rows), "format": "header",
            "domain": domain, "domains": [domain.lstrip(".")]}
