"""Browser cookie discovery, so nobody has to hand-make a cookies.txt.

There is no such thing as a synthetic session cookie: sites validate sessions
server-side, so a fabricated value authenticates nothing. What we *can* remove
is all the manual work. This module finds the browsers installed on the machine,
actually tries to read cookies out of each one, and reports which ones work --
so "connect my account" becomes one click instead of a browser-extension hunt.

Windows note: since Chrome 127, Chromium browsers seal their cookie store with
app-bound encryption, so Chrome/Edge/Brave frequently fail while Firefox works.
Rather than guessing, we test and report the truth.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
                if dom.endswith(known):
                    seen.add(known)
        result["domains"] = sorted(seen)
        result["ok"] = result["count"] > 0
        if not result["ok"]:
            result["error"] = "No cookies could be read from this browser."
    except Exception as exc:
        result["error"] = _diagnose(f"{exc} {' '.join(log.errors)}", browser_id)
    if not result["ok"] and log.errors and not result["error"]:
        result["error"] = _diagnose(" ".join(log.errors), browser_id)
    return result


def _diagnose(text: str, browser_id: str) -> str:
    low = text.lower()
    if "could not copy" in low or "being used" in low or "permission denied" in low:
        return "Cookie database is locked. Close the browser completely and press Detect again."
    if "dpapi" in low or "decrypt" in low or "v20" in low or "app-bound" in low:
        return ("Windows seals this browser's cookies with app-bound encryption, so no external "
                "tool can read them. Use Sign in here instead, or Firefox.")
    if "unsupported" in low or "could not find" in low or "no such" in low or "not found" in low:
        return "No usable profile found for this browser."
    return (text.strip()[:180] or "Cookies could not be read.")


# ------------------------------------------------- built-in login browser
# The reliable escape hatch on Windows: instead of trying to decrypt a browser's
# sealed cookie store, we start a Chromium browser ourselves against a profile
# folder we own, with its DevTools port enabled. The user signs in normally in
# that window, and we ask the running browser for its own cookies. No extension,
# no cookies.txt, no decryption.

LOGIN_PORT = 9722


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
        import shutil as _sh
        candidates = [(n, Path(p)) for n, p in
                      (("Chrome", _sh.which("google-chrome") or ""),
                       ("Chromium", _sh.which("chromium") or ""),
                       ("Edge", _sh.which("microsoft-edge") or ""),
                       ("Brave", _sh.which("brave-browser") or "")) if p]
    for name, path in candidates:
        if path and path.exists() and not any(p == path for _, p in out):
            out.append((name, path))
    return out


def login_profile_dir(root: Path) -> Path:
    d = root / "login-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start_login_browser(root: Path, url: str = "https://www.instagram.com/accounts/login/") -> dict:
    """Open a browser we control, pointed at the site's login page."""
    bins = _chromium_binaries()
    if not bins:
        return {"ok": False, "error": "No Chrome, Edge, Brave or Vivaldi installation found. "
                                      "Install one, or use Firefox with Detect browsers."}
    name, exe = bins[0]
    profile = login_profile_dir(root)
    args = [
        str(exe),
        f"--remote-debugging-port={LOGIN_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--new-window",
        url,
    ]
    try:
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         if os.name == "nt" else 0)
    except Exception as exc:
        return {"ok": False, "error": f"Could not start {name}: {exc}"}
    return {"ok": True, "browser": name, "port": LOGIN_PORT, "profile": str(profile),
            "message": f"{name} is opening. Sign in to the site in that window, then come "
                       f"back here and press \"I have signed in\"."}


def _cdp_all_cookies(port: int = LOGIN_PORT, timeout: float = 10.0) -> list[dict]:
    """Ask the running browser for its cookies over the DevTools protocol."""
    import json
    import urllib.request
    from websockets.sync.client import connect

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
        ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
    with connect(ws_url, open_timeout=timeout, close_timeout=timeout, max_size=64 * 1024 * 1024) as ws:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        while True:
            msg = json.loads(ws.recv(timeout=timeout))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "DevTools refused"))
                return msg.get("result", {}).get("cookies", [])


def harvest_login_cookies(dest: Path, port: int = LOGIN_PORT) -> dict:
    """Write the signed-in browser's cookies to a Netscape cookies.txt."""
    try:
        raw = _cdp_all_cookies(port)
    except Exception as exc:
        return {"ok": False, "error": "Could not reach the sign-in browser. Make sure the window "
                                      f"opened by \"Sign in here\" is still open. ({str(exc)[:120]})"}
    if not raw:
        return {"ok": False, "error": "That browser has no cookies yet -- sign in first, "
                                      "then press this again."}

    lines = ["# Netscape HTTP Cookie File", "# Captured by Media Toolkit"]
    for c in raw:
        domain = c.get("domain", "")
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires <= 0:
            expires = 2147483647          # session cookie -> keep it usable
        lines.append("\t".join([
            domain, include_sub, c.get("path", "/") or "/",
            "TRUE" if c.get("secure") else "FALSE", str(expires),
            c.get("name", ""), c.get("value", ""),
        ]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sites = sorted({d.lstrip(".") for d in (c.get("domain", "") for c in raw) if d})
    notable = [s for s in sites if any(k in s for k in
               ("instagram", "youtube", "tiktok", "x.com", "twitter", "facebook",
                "reddit", "vimeo", "patreon", "twitch"))]
    return {"ok": True, "path": str(dest), "cookies": len(raw),
            "sites": notable[:12], "total_domains": len(sites)}


def autodetect() -> dict:
    """Try every installed browser and rank the ones that actually work."""
    results = [test_browser(b["id"]) for b in installed()]
    for r, b in zip(results, installed()):
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
        where = f" with sessions for {', '.join(best['domains'][:4])}" if best["domains"] else ""
        return f"Using {best.get('label', best['browser'])}{where}. Nothing else to do."
    locked = [r for r in results if "locked" in r["error"].lower()]
    if locked:
        names = ", ".join(r.get("label", r["browser"]) for r in locked)
        return f"Close {names} and press Detect again -- the cookie file is locked while it runs."
    if results:
        return ("None of your browsers would hand over cookies. Windows encrypts Chrome and "
                "Edge cookie stores; installing Firefox and signing in there is the "
                "reliable fix. You can also paste a cookies.txt below.")
    return "No browsers found. Paste a cookies.txt below instead."


def import_text(raw: str, dest: Path) -> dict:
    """Accept a pasted cookies.txt, or a raw `name=value; name=value` header,
    and write a proper Netscape cookie file so the user never edits one by hand.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Nothing pasted")

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    looks_netscape = any(ln.startswith("#") for ln in lines[:3]) or \
        any(len(ln.split("\t")) >= 7 for ln in lines)

    dest.parent.mkdir(parents=True, exist_ok=True)
    if looks_netscape:
        body = raw if raw.startswith("# ") or raw.startswith("#HttpOnly") or "Netscape" in lines[0] \
            else "# Netscape HTTP Cookie File\n" + raw
        dest.write_text(body.rstrip() + "\n", encoding="utf-8")
        count = sum(1 for ln in body.splitlines()
                    if ln.strip() and not ln.startswith("#") and len(ln.split("\t")) >= 7)
        return {"path": str(dest), "cookies": count, "format": "netscape"}

    # Header form: "sessionid=abc; csrftoken=def"
    pairs = [p.strip() for p in raw.replace("\n", ";").split(";") if "=" in p]
    if not pairs:
        raise ValueError("Could not recognise that as cookies. Paste either a cookies.txt "
                         "file or a 'name=value; name=value' cookie header.")
    domain = ".instagram.com"
    for hint, dom in (("instagram", ".instagram.com"), ("youtube", ".youtube.com"),
                      ("tiktok", ".tiktok.com"), ("twitter", ".x.com"), ("reddit", ".reddit.com")):
        if hint in raw.lower():
            domain = dom
            break
    out = ["# Netscape HTTP Cookie File", "# Written by Media Toolkit"]
    for pair in pairs:
        name, _, value = pair.partition("=")
        out.append("\t".join([domain, "TRUE", "/", "TRUE", "2147483647",
                              name.strip(), value.strip()]))
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"path": str(dest), "cookies": len(pairs), "format": "header",
            "domain": domain,
            "note": f"Saved for {domain}. If you meant a different site, paste a full cookies.txt."}
