"""Keep site support current without reinstalling the app.

Sites change weekly and yt-dlp follows them, so a yt-dlp frozen into the
installer goes stale long before the app itself needs a new release. The
installed build has no pip, so an update is the plain PyPI wheels of yt-dlp
and the matching yt-dlp-ejs (YouTube's JavaScript challenge solver), checked
against the SHA-256 that PyPI publishes, unpacked into
DATA_ROOT/runtime/yt-dlp.

activate() puts that folder at the front of sys.path at startup. It must run
before anything imports yt_dlp: once a module is imported, the process keeps
it. It works in the frozen build because PyInstaller 6 serves its bundled
modules through a path-entry finder tied to the _internal folder's sys.path
entry, so an earlier sys.path entry with a real yt_dlp package wins, exactly
as it would in a source checkout.

A copy that is not newer than the bundled one is ignored (and deleted), so
installing a new app version that bundles a newer yt-dlp never gets shadowed
by an older download.

Another Media Toolkit process may be importing from that folder: yt-dlp
loads most extractors only when a link needs them, long after startup, and a
second launch runs activate() before it finds the running instance and hands
over to it. So a process that uses the downloaded copy keeps a file in it
open for as long as it runs (_pin), and the folder is only ever replaced or
deleted by renaming it first, which Windows refuses while that file is open.
A copy in use is therefore never pulled out from under a running app; the
swap simply waits for the next start.

check_app_update() asks GitHub for the newest published release. Both checks
run only when the user asks; nothing here phones home on its own.
"""
from __future__ import annotations

import atexit
import importlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from threading import Lock

from . import __version__, config

REPO = "AnotherAH/media-toolkit"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
PYPI_JSON = "https://pypi.org/pypi/{}/json"
PYPI_RELEASE_JSON = "https://pypi.org/pypi/{}/{}/json"

MARKER = "INSTALLED.json"
PACKAGES = ("yt_dlp", "yt_dlp_ejs")
# How long activate() waits for another process to let go of the current copy
# before a staged update is left for the next start (see _promote_waiting).
PROMOTE_WAIT = 4.0

_update_lock = Lock()
# What activate() decided, for /api/about and the log.
_activation: dict = {"active": False, "version": "", "reason": "not run"}
# Open handle on the marker of the copy this process imports from (see _pin).
_hold = None


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_dir() -> Path:
    return config.RUNTIME_DIR / "yt-dlp"


def _staged_dir() -> Path:
    return config.RUNTIME_DIR / "yt-dlp.new"


def parse_version(text: str) -> tuple:
    """'2026.08.19' and '2026.8.19.1' compare as dates; 'v1.2.0' as 1.2.0.

    Anything that is not a number sorts below every number, so a malformed
    version never looks newer than a real one."""
    parts = []
    for piece in re.split(r"[.\-+]", (text or "").strip().lstrip("vV")):
        parts.append(int(piece) if piece.isdigit() else -1)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _version_in_file(path: Path) -> str:
    try:
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", path.read_text("utf-8"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def _valid_copy(folder: Path) -> str:
    """Version of a complete downloaded copy in folder, or '' if it is not one."""
    if not (folder / MARKER).is_file():
        return ""
    for pkg in PACKAGES:
        if not (folder / pkg / "__init__.py").is_file():
            return ""
    return _version_in_file(folder / "yt_dlp" / "version.py")


def bundled_version(exclude: Path | None = None) -> str:
    """Version of the yt_dlp the app would import without an update.

    Read without importing yt_dlp: importing its version module runs the
    package __init__, which would pin the bundled copy for the whole process.
    Each sys.path entry's own finder is asked for yt_dlp.version and its code
    is run in an empty namespace (the module only assigns constants). That
    finder is PyInstaller's in the frozen build and FileFinder from source.
    """
    skip = {os.path.normcase(str(exclude))} if exclude else set()
    for entry in list(sys.path):
        if not entry or os.path.normcase(entry) in skip:
            continue
        try:
            finder = pkgutil.get_importer(os.path.join(entry, "yt_dlp"))
            spec = finder.find_spec("yt_dlp.version") if finder else None
            if spec is None or spec.loader is None:
                continue
            code = spec.loader.get_code("yt_dlp.version")
            ns: dict = {}
            exec(code, ns)                              # constants only
            if ns.get("__version__"):
                return str(ns["__version__"])
        except Exception:
            continue
    return ""


def _pin(folder: Path) -> bool:
    """Keep folder in place for as long as this process may import from it.

    Python opens files without delete sharing, and Windows will not rename a
    folder while a file inside it is open that way. Holding the marker open
    therefore stops another Media Toolkit process from swapping or deleting
    this copy (every such change starts with a rename, see _retire). Returns
    False when the folder has no marker, i.e. no complete copy to use."""
    global _hold
    _unpin()
    try:
        _hold = open(folder / MARKER, "rb")
    except OSError:
        return False
    return True


def _unpin() -> None:
    global _hold
    if _hold is not None:
        try:
            _hold.close()
        except OSError:
            pass
        _hold = None


# Let go as soon as the interpreter shuts down rather than when the process
# is finally gone, so a restarted app can swap in a staged update.
atexit.register(_unpin)


def _retire(folder: Path) -> bool:
    """Delete a copy, but only if no running process uses it: it is renamed
    first, which fails while another process has it pinned, and only the
    renamed folder is deleted. Returns False when it had to stay."""
    if not folder.exists():
        return True
    gone = config.RUNTIME_DIR / f".retired-{os.getpid()}-{time.time_ns()}"
    try:
        os.replace(folder, gone)
    except OSError:
        return False
    shutil.rmtree(gone, ignore_errors=True)
    return True


def _sweep_retired() -> None:
    """Finish deletions an earlier start could not complete."""
    try:
        leftovers = list(config.RUNTIME_DIR.glob(".retired-*"))
    except OSError:
        return
    for p in leftovers:
        shutil.rmtree(p, ignore_errors=True)


def _promote_staged() -> bool:
    """Move a finished yt-dlp.new into place. Never called while this process
    imports from runtime_dir(), so no module can mix versions; raises OSError
    (and changes nothing) while another process still uses the current copy.
    Returns True when a staged copy was moved in."""
    staged, live = _staged_dir(), runtime_dir()
    if not _valid_copy(staged):
        return False
    old = config.RUNTIME_DIR / f".retired-{os.getpid()}-{time.time_ns()}"
    if live.exists():
        os.replace(live, old)               # refused while another process pins it
    try:
        os.replace(staged, live)
    except OSError:
        if old.exists() and not live.exists():
            os.replace(old, live)           # put the working copy back
        raise
    shutil.rmtree(old, ignore_errors=True)
    return True


def _promote_waiting(seconds: float) -> None:
    """_promote_staged(), retried for a few seconds while the copy is still
    pinned. After "Restart now" the old process can hold its pin for a moment
    after the new one starts; without the wait the new process would run the
    old version for a whole session. Returns at once when nothing is staged."""
    end = time.monotonic() + seconds
    while True:
        try:
            _promote_staged()
            return
        except OSError:
            if time.monotonic() >= end:
                return                      # still in use elsewhere: next start
            time.sleep(0.25)


def _active_from_runtime() -> bool:
    mod = sys.modules.get("yt_dlp")
    origin = getattr(mod, "__file__", "") or ""
    return bool(origin) and os.path.normcase(str(runtime_dir())) in os.path.normcase(origin)


def activate() -> dict:
    """Use a downloaded yt-dlp if there is a newer one than the bundled copy.

    Call once at startup, before anything imports yt_dlp or yt_dlp_ejs.
    Never raises: a broken download must not stop the app from starting.
    Safe while another instance runs from the same data folder: a copy that
    process uses is never moved or deleted (see the module docstring).
    """
    global _activation
    try:
        if "yt_dlp" in sys.modules:
            _activation = {"active": False, "version": "",
                           "reason": "yt-dlp was imported before activate()"}
            return dict(_activation)
        _sweep_retired()
        _promote_waiting(PROMOTE_WAIT)
        live = runtime_dir()
        # Pin before reading anything, so the copy that is checked is the one
        # that gets imported, even if another process is mid-swap.
        if not _pin(live):
            _activation = {"active": False, "version": "", "reason": "no downloaded update"}
            return dict(_activation)
        version = _valid_copy(live)
        if not version:
            _unpin()
            _activation = {"active": False, "version": "", "reason": "no downloaded update"}
            return dict(_activation)
        bundled = bundled_version(exclude=live)
        if bundled and parse_version(version) <= parse_version(bundled):
            # The app itself was updated past the download: drop the old copy
            # (unless another running instance still imports from it).
            _unpin()
            _retire(live)
            _activation = {"active": False, "version": bundled,
                           "reason": f"bundled {bundled} is not older than downloaded {version}"}
            return dict(_activation)
        path = str(live)
        if path not in sys.path:
            sys.path.insert(0, path)
        importlib.invalidate_caches()
        problem = _load_check(live, version)
        if problem:
            # Fall back to the bundled copy for this start and every later one:
            # a download that cannot load must never keep the app from opening.
            _unload(path)
            _unpin()
            _quarantine(live)
            _activation = {"active": False, "version": bundled,
                           "reason": f"downloaded {version} did not load ({problem}); using bundled"}
            return dict(_activation)
        _activation = {"active": True, "version": version, "reason": f"bundled {bundled or '?'}"}
    except Exception as exc:                            # never block startup
        if not _active_from_runtime():
            _unpin()
        _activation = {"active": False, "version": "", "reason": f"error: {exc}"}
    return dict(_activation)


def _load_check(live: Path, version: str) -> str:
    """Import the downloaded yt-dlp now, while falling back is still possible.

    The app imports yt_dlp moments later anyway, so this costs nothing. It
    catches an update that needs something this build lacks (a new
    dependency, a newer Python) before it can stop the app from starting.
    Returns '' when it loaded from live, else what went wrong."""
    try:
        mod = importlib.import_module("yt_dlp")
        got = importlib.import_module("yt_dlp.version").__version__
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    origin = os.path.normcase(str(Path(getattr(mod, "__file__", "") or "").resolve()))
    if not origin.startswith(os.path.normcase(str(live.resolve())) + os.sep):
        return f"yt_dlp came from {origin or 'nowhere'}"
    if parse_version(got) != parse_version(version):
        return f"it reports version {got}"
    return ""


def _unload(path: str) -> None:
    """Forget a half-imported download so the bundled copy imports cleanly."""
    while path in sys.path:
        sys.path.remove(path)
    for name in list(sys.modules):
        if name.split(".", 1)[0] in PACKAGES:
            del sys.modules[name]
    importlib.invalidate_caches()


def _quarantine(live: Path) -> None:
    """Move a copy that failed to load aside (only the latest one is kept,
    for the log's sake) so the next start does not try it again. If another
    running instance still has it pinned, it stays where it is."""
    broken = config.RUNTIME_DIR / "yt-dlp.broken"
    _retire(broken)
    try:
        os.replace(live, broken)
    except OSError:
        pass


def activation() -> dict:
    return dict(_activation)


def current_version() -> str:
    """The yt-dlp version this process uses (or will use once imported)."""
    mod = sys.modules.get("yt_dlp.version")
    if mod is not None and getattr(mod, "__version__", ""):
        return mod.__version__
    try:
        from yt_dlp.version import __version__ as v
        return v
    except Exception:
        return _activation.get("version") or bundled_version()


# ------------------------------------------------------------- update yt-dlp

def _get_json(url: str, timeout: float = 20) -> dict:
    from .assets import opener
    req = urllib.request.Request(url, headers={"User-Agent": f"MediaToolkit/{__version__}",
                                               "Accept": "application/json"})
    with opener().open(req, timeout=timeout) as r:
        return json.loads(r.read())


def python_ok(requires: str, version: tuple | None = None) -> bool:
    """Whether this Python meets a Requires-Python string like '>=3.10' or
    '>=3.9,<4'.

    The installed app runs on the Python it was built with, for years. When
    yt-dlp one day drops that Python, its newest release must not be
    installed into a build that cannot run it. A clause this does not
    understand never blocks an update."""
    ours = tuple(version or sys.version_info[:3])
    for clause in (requires or "").split(","):
        m = re.fullmatch(r"\s*(>=|<=|>|<|==|!=)\s*(\d+(?:\.\d+)*)(\.\*)?\s*", clause)
        if not m:
            continue
        op, text, star = m.groups()
        want = tuple(int(x) for x in text.split("."))
        if star:                                    # ==3.13.* / !=3.8.*
            same = ours[:len(want)] == want
            if (op == "==" and not same) or (op == "!=" and same):
                return False
            continue
        want += (0,) * (3 - len(want))
        have = ours + (0,) * (3 - len(ours))
        ok = {">=": have >= want, "<=": have <= want, ">": have > want,
              "<": have < want, "==": have == want, "!=": have != want}[op]
        if not ok:
            return False
    return True


def _wheel(data: dict) -> tuple[str, str, str]:
    """(url, sha256, filename) of the pure-Python wheel in a PyPI JSON reply."""
    for entry in data.get("urls", []):
        name = entry.get("filename", "")
        if name.endswith("-py3-none-any.whl"):
            return entry["url"], entry["digests"]["sha256"], name
    raise RuntimeError("PyPI has no plain wheel for this release.")


def _ejs_requirement(wheel: Path) -> str:
    """The yt-dlp-ejs version this yt-dlp wants (its 'default' extra pins it)."""
    with zipfile.ZipFile(wheel) as zf:
        meta = next((n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), None)
        text = zf.read(meta).decode("utf-8", "replace") if meta else ""
    m = re.search(r"^Requires-Dist:\s*yt-dlp-ejs\s*==\s*([\w.]+)", text, re.M)
    return m.group(1) if m else ""


def _unpack(wheel: Path, dest: Path) -> None:
    """Extract the importable packages and their metadata, nothing else, and
    refuse any member that would land outside dest."""
    root = dest.resolve()
    with zipfile.ZipFile(wheel) as zf:
        for member in zf.infolist():
            top = member.filename.split("/", 1)[0]
            if top not in PACKAGES and not top.endswith(".dist-info"):
                continue
            target = (dest / member.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError("The download contains an unsafe file path.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _precompile(folder: Path) -> None:
    """Byte-compile once now, so the next start does not compile ~1,800
    extractor modules while the window waits."""
    try:
        import compileall
        compileall.compile_dir(str(folder), quiet=1, workers=1)
    except Exception:
        pass                                            # only a speed-up


def _reason(exc: BaseException) -> str:
    from .assets import PackError, _friendly
    if isinstance(exc, (PackError, urllib.error.URLError, OSError, zipfile.BadZipFile)):
        return _friendly(exc)
    return str(exc)[:200] or type(exc).__name__


def _result(ok: bool, version: str, message: str, restart: bool) -> dict:
    return {"ok": ok, "version": version, "message": message, "restart_required": restart}


def update() -> dict:
    """Fetch the newest yt-dlp. Returns {ok, version, message, restart_required}.

    Frozen: PyPI wheels into DATA_ROOT/runtime/yt-dlp (see module docstring).
    From source: pip into the running environment, which is what a developer
    checkout expects.
    """
    if not _update_lock.acquire(blocking=False):
        return _result(False, current_version(), "An update is already running.", False)
    try:
        if not _frozen():
            return _update_with_pip()
        return _update_frozen()
    finally:
        _update_lock.release()


def _update_frozen() -> dict:
    from .assets import download
    current = current_version()
    work = config.RUNTIME_DIR / ".downloads"
    staged = _staged_dir()
    files: list[Path] = []
    try:
        info = _get_json(PYPI_JSON.format("yt-dlp"))
        latest = info["info"]["version"]
        pending = _valid_copy(staged) or _valid_copy(runtime_dir())
        if parse_version(latest) <= parse_version(current):
            return _result(True, current, f"Up to date ({current})", False)
        if pending and parse_version(pending) >= parse_version(latest):
            return _result(True, pending, f"Updated to {pending}. Restart Media Toolkit to use it.", True)
        if not python_ok(info["info"].get("requires_python") or ""):
            return _result(False, current, "Couldn't update: the newest site support needs a "
                                           "newer version of Media Toolkit.", False)

        work.mkdir(parents=True, exist_ok=True)
        url, sha, name = _wheel(info)
        ytdlp = download(url, work / name, sha)
        files.append(ytdlp)

        ejs_version = _ejs_requirement(ytdlp)
        ejs_info = _get_json(PYPI_RELEASE_JSON.format("yt-dlp-ejs", ejs_version) if ejs_version
                             else PYPI_JSON.format("yt-dlp-ejs"))
        url, sha, name = _wheel(ejs_info)
        ejs = download(url, work / name, sha)
        files.append(ejs)

        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
        _unpack(ytdlp, staged)
        _unpack(ejs, staged)
        got = _version_in_file(staged / "yt_dlp" / "version.py")
        if parse_version(got) != parse_version(latest):
            raise RuntimeError(f"The download holds yt-dlp {got or '?'}, not {latest}.")
        _precompile(staged)
        (staged / MARKER).write_text(json.dumps({
            "yt_dlp": latest, "yt_dlp_ejs": ejs_info["info"]["version"],
            "installed": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2), "utf-8")

        if not _active_from_runtime():
            try:
                _promote_staged()       # nothing here imports from there: swap now
            except OSError:
                pass                    # another process uses it: activate() swaps later
        # else activate() swaps it in at the next start, before any import.
        # yt-dlp's own spelling (2026.08.19), as the About page shows it.
        return _result(True, got, f"Updated to {got}. Restart Media Toolkit to use it.", True)
    except Exception as exc:
        if not _valid_copy(staged):
            shutil.rmtree(staged, ignore_errors=True)
        return _result(False, current, f"Couldn't update: {_reason(exc)}", False)
    finally:
        for f in files:
            f.unlink(missing_ok=True)


def _update_with_pip() -> dict:
    current = current_version()
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--no-input",
             "--disable-pip-version-check", "yt-dlp[default]"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600, **kw)
    except Exception as exc:
        return _result(False, current, f"Couldn't update: {_reason(exc)}", False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["pip failed"]
        return _result(False, current, f"Couldn't update: {tail[0][:200]}", False)
    try:
        after = subprocess.run(
            [sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
            capture_output=True, text=True, timeout=120, **kw).stdout.strip()
    except Exception:
        after = ""
    if not after or parse_version(after) == parse_version(current):
        return _result(True, current, f"Up to date ({current})", False)
    return _result(True, after, f"Updated to {after}. Restart Media Toolkit to use it.", True)


# ------------------------------------------------------------ app releases

def check_app_update() -> dict:
    """Compare this build with the newest published GitHub release.

    Returns {ok, current, latest, url, update_available, message}. A 404 is
    what GitHub answers while the repository is private or has no published
    release yet; that is "no update", not an error.
    """
    base = {"ok": True, "current": __version__, "latest": None, "url": RELEASES_PAGE,
            "update_available": False}
    try:
        data = _get_json(LATEST_API, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {**base, "message": "No releases are published yet."}
        if exc.code in (403, 429):
            return {**base, "ok": False,
                    "message": "GitHub is limiting update checks right now. Try again in an hour."}
        return {**base, "ok": False, "message": f"Couldn't check for updates (GitHub error {exc.code})."}
    except Exception:
        return {**base, "ok": False,
                "message": "Couldn't check for updates. Check your internet connection."}
    latest = str(data.get("tag_name") or "").lstrip("vV")
    url = data.get("html_url") or RELEASES_PAGE
    if not latest:
        return {**base, "message": "No releases are published yet."}
    newer = parse_version(latest) > parse_version(__version__)
    return {**base, "latest": latest, "url": url, "update_available": newer,
            "message": f"Version {latest} is available." if newer else "You're on the latest version."}
