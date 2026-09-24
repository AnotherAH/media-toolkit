"""app/updater.py: yt-dlp updates without pip, and the app release check.

Offline. Activation is checked in a child process, because it must happen
before yt_dlp is imported and this test process may already have imported it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

from app import __version__, assets, config, updater

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ helpers

def make_copy(folder: Path, version: str, marker: bool = True) -> Path:
    """A downloaded-update folder as update() leaves it."""
    (folder / "yt_dlp").mkdir(parents=True)
    (folder / "yt_dlp" / "__init__.py").write_text("FROM_RUNTIME = True\n", "utf-8")
    (folder / "yt_dlp" / "version.py").write_text(f"__version__ = '{version}'\n", "utf-8")
    (folder / "yt_dlp_ejs").mkdir()
    (folder / "yt_dlp_ejs" / "__init__.py").write_text("", "utf-8")
    if marker:
        (folder / updater.MARKER).write_text(json.dumps({"yt_dlp": version}), "utf-8")
    return folder


def run_child(home: Path, code: str) -> dict:
    env = dict(os.environ, MEDIA_TOOLKIT_HOME=str(home))
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


ACTIVATE_AND_IMPORT = """
import json, sys
from app import updater
res = updater.activate()
import yt_dlp
print(json.dumps({"res": res, "file": yt_dlp.__file__,
                  "runtime": getattr(yt_dlp, "FROM_RUNTIME", False)}))
"""


# ---------------------------------------------------------------- versions

@pytest.mark.parametrize("a,b", [("2026.08.19", "2026.9.1"), ("2026.8.19", "2026.08.19.1"),
                                 ("1.1.1", "1.2.0"), ("v1.2.0", "1.10.0"), ("garbage", "0.0.1")])
def test_parse_version_orders(a, b):
    assert updater.parse_version(a) < updater.parse_version(b)


def test_parse_version_equal_forms():
    assert updater.parse_version("2026.08.19") == updater.parse_version("2026.8.19")
    assert updater.parse_version("v1.2.0") == updater.parse_version("1.2")


def test_bundled_version_does_not_import_yt_dlp(tmp_path):
    res = run_child(tmp_path, "import json, sys\nfrom app import updater\n"
                              "v = updater.bundled_version()\n"
                              "print(json.dumps({'v': v, 'imported': 'yt_dlp' in sys.modules}))")
    import yt_dlp.version
    assert res["v"] == yt_dlp.version.__version__
    assert res["imported"] is False


# --------------------------------------------------------------- activate()

def test_activate_uses_a_newer_download(tmp_path):
    make_copy(tmp_path / "runtime" / "yt-dlp", "2999.01.01")
    res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["active"] is True and res["res"]["version"] == "2999.01.01"
    assert res["runtime"] is True
    assert str(tmp_path / "runtime" / "yt-dlp") in res["file"]


def test_activate_ignores_and_removes_an_older_download(tmp_path):
    live = make_copy(tmp_path / "runtime" / "yt-dlp", "2000.01.01")
    res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["active"] is False
    assert res["runtime"] is False
    assert not live.exists()


def test_activate_ignores_an_incomplete_download(tmp_path):
    make_copy(tmp_path / "runtime" / "yt-dlp", "2999.01.01", marker=False)
    res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["active"] is False and res["runtime"] is False


def test_activate_promotes_a_staged_update(tmp_path):
    make_copy(tmp_path / "runtime" / "yt-dlp", "2998.01.01")
    make_copy(tmp_path / "runtime" / "yt-dlp.new", "2999.01.01")
    res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["version"] == "2999.01.01" and res["runtime"] is True
    assert not (tmp_path / "runtime" / "yt-dlp.new").exists()


def test_activate_falls_back_when_the_download_cannot_load(tmp_path):
    # An update that needs something this build lacks must not stop the app
    # from starting: the bundled yt-dlp is used, and the copy is moved aside
    # so the next start does not try it again.
    live = make_copy(tmp_path / "runtime" / "yt-dlp", "2999.01.01")
    (live / "yt_dlp" / "__init__.py").write_text("import module_this_build_lacks\n", "utf-8")
    res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["active"] is False and "did not load" in res["res"]["reason"]
    assert res["runtime"] is False
    assert str(tmp_path / "runtime") not in res["file"]
    assert not live.exists() and (tmp_path / "runtime" / "yt-dlp.broken").is_dir()


windows_only = pytest.mark.skipif(os.name != "nt", reason="relies on Windows file sharing rules")


@windows_only
def test_activate_never_swaps_a_copy_another_instance_uses(tmp_path):
    # A second launch runs activate() before it finds the running instance.
    # The running instance imports extractors from runtime/yt-dlp for as long
    # as it runs, so a staged update must wait instead of renaming that
    # folder away (which would break every later import in that instance).
    live = make_copy(tmp_path / "runtime" / "yt-dlp", "2998.01.01")
    staged = make_copy(tmp_path / "runtime" / "yt-dlp.new", "2999.01.01")
    with open(live / updater.MARKER, "rb"):            # what _pin holds in the other process
        res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["version"] == "2998.01.01" and res["runtime"] is True
    assert (live / "yt_dlp" / "__init__.py").exists() and staged.is_dir()
    # Once that instance has gone, the next start swaps it in.
    res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert res["res"]["version"] == "2999.01.01" and not staged.exists()


@windows_only
def test_activate_waits_briefly_for_a_restarting_instance(tmp_path):
    # "Restart now": the old process may still hold its pin for a moment
    # after the new one starts. The new one waits for it instead of running
    # the old version for a whole session.
    live = make_copy(tmp_path / "runtime" / "yt-dlp", "2998.01.01")
    make_copy(tmp_path / "runtime" / "yt-dlp.new", "2999.01.01")
    env = dict(os.environ, MEDIA_TOOLKIT_HOME=str(tmp_path))
    fh = open(live / updater.MARKER, "rb")
    try:
        child = subprocess.Popen([sys.executable, "-c", ACTIVATE_AND_IMPORT], cwd=str(ROOT), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import time
        time.sleep(1.0)
    finally:
        fh.close()                                      # the old process exits
    out, err = child.communicate(timeout=120)
    assert child.returncode == 0, err
    res = json.loads(out.strip().splitlines()[-1])
    assert res["res"]["version"] == "2999.01.01" and res["runtime"] is True


@windows_only
def test_activate_never_deletes_a_copy_another_instance_uses(tmp_path):
    live = make_copy(tmp_path / "runtime" / "yt-dlp", "2000.01.01")    # older than bundled
    with open(live / updater.MARKER, "rb"):
        res = run_child(tmp_path, ACTIVATE_AND_IMPORT)
        assert res["res"]["active"] is False and res["runtime"] is False
        assert (live / "yt_dlp" / "version.py").exists()             # nothing pulled out
    run_child(tmp_path, ACTIVATE_AND_IMPORT)
    assert not live.exists()
    assert not list((tmp_path / "runtime").glob(".retired-*"))


def test_activate_keeps_the_copy_it_uses_pinned(tmp_path):
    make_copy(tmp_path / "runtime" / "yt-dlp", "2999.01.01")
    code = ("import json, os\nfrom app import updater\nres = updater.activate()\n"
            "live = updater.runtime_dir()\n"
            "try:\n    os.replace(live, live.with_name('moved'))\n    moved = True\n"
            "except OSError:\n    moved = False\n"
            "print(json.dumps({'active': res['active'], 'moved': moved}))")
    res = run_child(tmp_path, code)
    assert res["active"] is True
    if os.name == "nt":
        assert res["moved"] is False


def test_activate_is_a_no_op_after_yt_dlp_was_imported(tmp_path):
    make_copy(tmp_path / "runtime" / "yt-dlp", "2999.01.01")
    code = "import json, yt_dlp\nfrom app import updater\nprint(json.dumps(updater.activate()))"
    res = run_child(tmp_path, code)
    assert res["active"] is False and "imported" in res["reason"]


# ------------------------------------------------------------ update pieces

def _wheel(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def test_unpack_refuses_paths_outside_the_folder(tmp_path):
    w = _wheel(tmp_path / "evil.whl", {"yt_dlp/../../escape.py": "x"})
    with pytest.raises(RuntimeError):
        updater._unpack(w, tmp_path / "out")
    assert not (tmp_path / "escape.py").exists()


def test_unpack_keeps_only_packages_and_metadata(tmp_path):
    w = _wheel(tmp_path / "ok.whl", {"yt_dlp/__init__.py": "", "yt_dlp-1.dist-info/METADATA": "m",
                                     "share/man/yt-dlp.1": "man", "yt_dlp_ejs/x.js": "js"})
    updater._unpack(w, tmp_path / "out")
    got = sorted(p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*")
                 if p.is_file())
    assert got == ["yt_dlp-1.dist-info/METADATA", "yt_dlp/__init__.py", "yt_dlp_ejs/x.js"]


def test_ejs_requirement_from_metadata(tmp_path):
    meta = ("Metadata-Version: 2.4\nName: yt-dlp\n"
            "Requires-Dist: yt-dlp-ejs==0.9.1; extra == 'pin'\n"
            "Requires-Dist: yt-dlp-ejs==0.9.1; extra == 'default'\n")
    w = _wheel(tmp_path / "y.whl", {"yt_dlp-2026.9.1.dist-info/METADATA": meta})
    assert updater._ejs_requirement(w) == "0.9.1"


@pytest.fixture
def fake_pypi(tmp_path, monkeypatch):
    """PyPI JSON and wheel downloads served from local files."""
    new = "2999.1.1"
    ytdlp = _wheel(tmp_path / f"yt_dlp-{new}-py3-none-any.whl", {
        "yt_dlp/__init__.py": "FROM_RUNTIME = True\n",
        "yt_dlp/version.py": f"__version__ = '{new}'\n",
        f"yt_dlp-{new}.dist-info/METADATA":
            "Name: yt-dlp\nRequires-Dist: yt-dlp-ejs==0.9.9; extra == 'default'\n"})
    ejs = _wheel(tmp_path / "yt_dlp_ejs-0.9.9-py3-none-any.whl", {
        "yt_dlp_ejs/__init__.py": "", "yt_dlp_ejs/yt/solver/lib.min.js": "//js"})

    def entry(p: Path) -> dict:
        return {"filename": p.name, "url": str(p),
                "digests": {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}

    def get_json(url, timeout=20):
        if "yt-dlp-ejs/0.9.9" in url:
            return {"info": {"version": "0.9.9"}, "urls": [entry(ejs)]}
        if "/yt-dlp/json" in url:
            return {"info": {"version": new}, "urls": [entry(ytdlp)]}
        raise AssertionError(url)

    def download(url, dest, sha256, on_progress=None, size_hint=0, attempts=4):
        data = Path(url).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise assets.PackError("The download was damaged or is not the expected file. Try again.")
        dest.write_bytes(data)
        return dest

    monkeypatch.setattr(updater, "_get_json", get_json)
    monkeypatch.setattr(assets, "download", download)
    monkeypatch.setattr(updater, "_frozen", lambda: True)
    monkeypatch.setattr(updater, "_precompile", lambda folder: None)
    shutil.rmtree(config.RUNTIME_DIR, ignore_errors=True)
    yield new
    shutil.rmtree(config.RUNTIME_DIR, ignore_errors=True)


def test_update_frozen_installs_newest_wheels(fake_pypi):
    res = updater.update()
    assert res == {"ok": True, "version": fake_pypi,
                   "message": f"Updated to {fake_pypi}. Restart Media Toolkit to use it.",
                   "restart_required": True}
    live = updater.runtime_dir()
    assert updater._valid_copy(live) == fake_pypi
    marker = json.loads((live / updater.MARKER).read_text())
    assert marker["yt_dlp_ejs"] == "0.9.9"
    assert not updater._staged_dir().exists()
    # A second click before restarting does not download again.
    again = updater.update()
    assert again["ok"] and again["restart_required"] and again["version"] == fake_pypi


def test_update_frozen_says_up_to_date(fake_pypi, monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: fake_pypi)
    res = updater.update()
    assert res == {"ok": True, "version": fake_pypi, "message": f"Up to date ({fake_pypi})",
                   "restart_required": False}


def test_update_frozen_reports_a_bad_download(fake_pypi, monkeypatch):
    def broken(*a, **k):
        raise assets.PackError("The download was damaged or is not the expected file. Try again.")
    monkeypatch.setattr(assets, "download", broken)
    res = updater.update()
    assert res["ok"] is False and res["restart_required"] is False
    assert res["message"].startswith("Couldn't update: ")
    assert not updater._staged_dir().exists() and not updater.runtime_dir().exists()


@pytest.mark.parametrize("requires,ok", [
    ("", True), (">=3.10", True), (">=3.9,<4", True), (">=3.14", False), ("<3.13", False),
    ("<=3.13", True), (">3.13", False), ("!=3.13.*", False), ("==3.13.*", True),
    (">=3.10, !=3.11.*", True), ("~=3.10", True), ("junk", True)])
def test_python_ok(requires, ok):
    assert updater.python_ok(requires, (3, 13, 0)) is ok


def test_update_frozen_refuses_a_release_this_python_cannot_run(fake_pypi, monkeypatch):
    real = updater._get_json

    def newer_python_only(url, timeout=20):
        data = real(url, timeout)
        data["info"]["requires_python"] = ">=3.99"
        return data
    monkeypatch.setattr(updater, "_get_json", newer_python_only)
    monkeypatch.setattr(assets, "download", lambda *a, **k: pytest.fail("must not download"))
    res = updater.update()
    assert res["ok"] is False and res["restart_required"] is False
    assert res["message"] == ("Couldn't update: the newest site support needs a newer "
                              "version of Media Toolkit.")
    assert not updater.runtime_dir().exists()


def test_update_messages_have_no_jargon(fake_pypi):
    res = updater.update()
    assert "pip" not in res["message"] and "—" not in res["message"]


# ------------------------------------------------------------ app releases

def _http_error(code):
    return urllib.error.HTTPError("https://api.github.com", code, "x", {}, None)


def test_check_app_update_while_repo_is_private(monkeypatch):
    def raise_404(url, timeout=20):
        raise _http_error(404)
    monkeypatch.setattr(updater, "_get_json", raise_404)
    res = updater.check_app_update()
    assert res["update_available"] is False and res["current"] == __version__
    assert res["message"] == "No releases are published yet."
    assert res["url"] == updater.RELEASES_PAGE


def test_check_app_update_newer_release(monkeypatch):
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=20: {
        "tag_name": "v99.0.0", "html_url": "https://github.com/AnotherAH/media-toolkit/releases/tag/v99.0.0"})
    res = updater.check_app_update()
    assert res["update_available"] is True and res["latest"] == "99.0.0"
    assert res["message"] == "Version 99.0.0 is available."
    assert res["url"].endswith("/v99.0.0")


def test_check_app_update_same_version(monkeypatch):
    monkeypatch.setattr(updater, "_get_json", lambda url, timeout=20: {"tag_name": f"v{__version__}"})
    res = updater.check_app_update()
    assert res["update_available"] is False and res["message"] == "You're on the latest version."


def test_check_app_update_offline(monkeypatch):
    def offline(url, timeout=20):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(updater, "_get_json", offline)
    res = updater.check_app_update()
    assert res["ok"] is False and res["update_available"] is False
    assert "internet" in res["message"]


@pytest.mark.network
def test_check_app_update_live():
    res = updater.check_app_update()
    assert res["current"] == __version__ and "message" in res
