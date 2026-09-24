"""The local server's security model (app/main.py).

Threat model: every web page the user visits can send requests to 127.0.0.1,
and a DNS-rebinding page can read the answers. So the Host must name this
server, a cross-site Origin or Sec-Fetch-Site is refused, and anything that
changes state needs the per-launch token from index.html. Nothing a request
says may choose where files are written or which program runs.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, jobs
from app import main as appmain

PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    """Every test starts in the shipped mode: loopback, token required."""
    monkeypatch.setattr(appmain, "DEV_NO_TOKEN", False)
    monkeypatch.setattr(appmain, "REMOTE", False)


@pytest.fixture
def client():
    return TestClient(appmain.app, base_url=BASE)


def tok(**extra) -> dict:
    return {"X-MT-Token": appmain.TOKEN, **extra}


# ------------------------------------------------------------------ Host

def test_own_host_is_served(client):
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/jobs", headers={"Host": f"localhost:{PORT}"}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1:9999",
                                  "127.0.0.1", f"127.0.0.2:{PORT}", ""])
def test_foreign_host_is_refused(client, host):
    """DNS rebinding: the attacker's name resolves to 127.0.0.1 but the Host
    header still carries the attacker's name."""
    r = client.get("/api/settings", headers={"Host": host})
    assert r.status_code == 403
    assert "proxy" not in r.text                     # no settings leaked


def test_host_check_covers_the_page_too(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


# ---------------------------------------------------------------- Origin

def test_cross_origin_is_refused_even_for_gets(client):
    r = client.get("/api/probe", params={"url": "http://192.168.1.1/"},
                   headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_origin_passes(client):
    r = client.get("/api/jobs", headers={"Origin": BASE})
    assert r.status_code == 200


def test_null_origin_is_refused(client):
    assert client.get("/api/jobs", headers={"Origin": "null"}).status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_metadata_is_refused(client, site, monkeypatch):
    """An <img src> or no-cors fetch sends no Origin, but the browser marks
    it cross-site. The link preview must not become a proxy into the LAN."""
    called = []
    monkeypatch.setattr(appmain.media, "probe", lambda url: called.append(url) or {})
    r = client.get("/api/probe", params={"url": "http://192.168.1.1/admin"},
                   headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    assert called == []


def test_page_navigation_from_elsewhere_still_loads(client):
    r = client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


# ----------------------------------------------------------------- token

def test_state_change_needs_the_token(client):
    before = config.get()["rate_limit"]
    assert client.post("/api/settings", json={"rate_limit": "5M"}).status_code == 403
    assert client.post("/api/settings", json={"rate_limit": "5M"},
                       headers={"X-MT-Token": "wrong"}).status_code == 403
    assert config.get()["rate_limit"] == before
    r = client.post("/api/settings", json={"rate_limit": before or ""}, headers=tok())
    assert r.status_code == 200


@pytest.mark.parametrize("method,path", [
    ("post", "/api/jobs"), ("post", "/api/jobs/clear"), ("post", "/api/cookies/clear"),
    ("post", "/api/cookies/harvest"), ("post", "/api/cookies/detect"),
    ("post", "/api/models/repair"), ("post", "/api/update-ytdlp"), ("post", "/api/open"),
    ("post", "/api/reveal"), ("post", "/api/open-url"), ("post", "/api/heartbeat"),
    ("delete", "/api/jobs/j1"), ("post", "/api/transcribe-file"), ("post", "/api/packs/gpu"),
])
def test_every_mutating_route_is_gated(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 403, path


def test_goodbye_is_exempt_because_beacons_cannot_set_headers(client):
    assert client.post("/api/goodbye").status_code == 200


def test_dev_mode_tolerates_a_missing_token_but_not_a_wrong_one(client, monkeypatch):
    monkeypatch.setattr(appmain, "DEV_NO_TOKEN", True)
    assert client.post("/api/heartbeat").status_code == 200
    assert client.post("/api/heartbeat", headers={"X-MT-Token": "nope"}).status_code == 403
    # Host and Origin are still enforced in dev mode.
    assert client.post("/api/heartbeat", headers={"Host": "evil.example"}).status_code == 403


def test_index_carries_the_token_and_is_not_cached(client):
    r = client.get("/")
    assert r.status_code == 200
    assert f'<meta name="mt-token" content="{appmain.TOKEN}">' in r.text
    assert "no-store" in r.headers["cache-control"]
    assert r.headers["x-frame-options"] == "DENY"


def test_token_is_random_and_long():
    assert len(appmain.TOKEN) >= 32


def test_instance_endpoint_needs_the_token(client):
    assert client.get("/api/instance").status_code == 403
    r = client.get("/api/instance", headers=tok())
    assert r.status_code == 200 and r.json()["pid"] == os.getpid()


# ------------------------------------------------------------ remote mode

def test_remote_mode_requires_the_token_everywhere(client, monkeypatch):
    monkeypatch.setattr(appmain, "REMOTE", True)
    lan = {"Host": f"192.168.1.20:{PORT}"}
    assert client.get("/api/jobs", headers=lan).status_code == 403
    assert client.get("/", headers=lan).status_code == 403
    assert client.get("/api/jobs", params={"token": appmain.TOKEN}, headers=lan).status_code == 200
    page = client.get("/", params={"token": appmain.TOKEN}, headers=lan)
    assert page.status_code == 200 and "mt_token" in page.headers.get("set-cookie", "")
    assert client.get("/api/jobs", headers={**lan, "Cookie": f"mt_token={appmain.TOKEN}"}) \
        .status_code == 200
    # A state change still needs the header, not just the cookie.
    assert client.post("/api/heartbeat", headers={**lan, "Cookie": f"mt_token={appmain.TOKEN}"}) \
        .status_code == 403
    assert client.post("/api/heartbeat", headers={**lan, **tok()}).status_code == 200


# ------------------------------------------------------ files on the disk

def test_file_download_route_is_gone(client):
    assert client.get("/api/file", params={"path": "C:/Windows/win.ini"}).status_code == 404


@pytest.fixture
def opened(monkeypatch):
    calls = []
    monkeypatch.setattr(appmain, "_shell_open", lambda p: calls.append(Path(p)))
    return calls


def test_open_refuses_paths_outside_the_folders(client, opened, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    for path in (str(outside), "C:/Windows/win.ini", "../../etc/passwd", ""):
        r = client.post("/api/open", json={"path": path}, headers=tok())
        assert r.status_code in (400, 403), path
    assert opened == []


def test_unc_paths_are_judged_without_touching_the_network(client, opened):
    started = time.time()
    r = client.post("/api/open", json={"path": r"\\10.255.255.1\share\x.txt"}, headers=tok())
    assert r.status_code == 403
    assert time.time() - started < 2           # no SMB connection attempt
    r = client.post("/api/reveal", json={"path": "//10.255.255.1/share"}, headers=tok())
    assert r.status_code == 403


def test_open_plays_a_file_in_the_download_folder(client, opened):
    folder = Path(config.get()["download_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "clip.mp4"
    video.write_bytes(b"\x00")
    try:
        r = client.post("/api/open", json={"path": str(video)}, headers=tok())
        assert r.status_code == 200 and opened == [video.resolve()]
        missing = client.post("/api/open", json={"path": str(folder / "gone.mp4")}, headers=tok())
        assert missing.status_code == 404
    finally:
        video.unlink()


@pytest.mark.parametrize("name", ["setup.exe", "run.BAT", "x.ps1", "y.lnk", "z.hta", "w.js",
                                  "help.chm", "pkg.msix", "noext", "a.mp4.exe"])
def test_open_never_runs_programs(client, opened, name):
    folder = Path(config.get()["download_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_bytes(b"MZ")
    try:
        r = client.post("/api/open", json={"path": str(f)}, headers=tok())
        assert r.status_code == 403
        assert opened == []
    finally:
        f.unlink()


def test_url_shortcut_must_point_to_a_web_page(client, opened):
    folder = Path(config.get()["download_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    good, bad = folder / "good.url", folder / "bad.url"
    good.write_text("[InternetShortcut]\nURL=https://www.youtube.com/watch?v=aqz-KE-bpKQ\n")
    bad.write_text("[InternetShortcut]\nURL=file:///C:/Windows/System32/calc.exe\n")
    try:
        assert client.post("/api/open", json={"path": str(good)}, headers=tok()).status_code == 200
        assert client.post("/api/open", json={"path": str(bad)}, headers=tok()).status_code == 403
    finally:
        good.unlink()
        bad.unlink()


def test_open_allows_the_data_folder_for_the_log(client, opened):
    config.LOG_PATH.write_text("log", encoding="utf-8") if not config.LOG_PATH.exists() else None
    r = client.post("/api/open", json={"path": str(config.LOG_PATH)}, headers=tok())
    assert r.status_code == 200


def test_reveal_is_confined_too(client, monkeypatch, tmp_path):
    popen = []
    monkeypatch.setattr(appmain.subprocess, "Popen", lambda *a, **k: popen.append(a))
    monkeypatch.setattr(appmain.os, "startfile", lambda p: popen.append(p), raising=False)
    r = client.post("/api/reveal", json={"path": str(tmp_path)}, headers=tok())
    assert r.status_code == 403 and popen == []
    folder = config.get()["download_dir"]
    r = client.post("/api/reveal", json={"path": folder}, headers=tok())
    assert r.status_code == 200 and popen


def test_a_jobs_own_file_can_be_revealed_after_the_folder_changed(client, monkeypatch, tmp_path):
    """Show in folder must keep working for files a job recorded, even when
    they are no longer inside the current download folder."""
    monkeypatch.setattr(appmain.subprocess, "Popen", lambda *a, **k: None)
    f = tmp_path / "old download.mp4"
    f.write_bytes(b"\x00")
    jid = jobs.create("download", "https://example.com/v")["id"]
    jobs.add_file(jid, str(f))
    try:
        assert client.post("/api/reveal", json={"path": str(f)}, headers=tok()).status_code == 200
        sibling = tmp_path / "other.txt"
        sibling.write_text("x")
        assert client.post("/api/reveal", json={"path": str(sibling)},
                           headers=tok()).status_code == 403
    finally:
        with jobs._lock:
            jobs._forget(jid)


# ------------------------------------------------------------ web pages

@pytest.fixture
def browser(monkeypatch):
    import webbrowser
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: calls.append(url))
    return calls


@pytest.mark.parametrize("url", [
    "https://nodejs.org/", "https://nodejs.org/en/download",
    "https://github.com/AnotherAH/media-toolkit",
    "https://github.com/AnotherAH/media-toolkit/releases",
    "https://github.com/AnotherAH/media-toolkit/releases/tag/v1.2.0",
])
def test_open_url_allows_the_apps_own_pages(client, browser, url):
    assert client.post("/api/open-url", json={"url": url}, headers=tok()).status_code == 200
    assert browser == [url]


@pytest.mark.parametrize("url", [
    "https://evil.example/", "http://nodejs.org/", "https://nodejs.org.evil.example/",
    "https://github.com/someone-else/repo", "https://github.com/AnotherAH/media-toolkit-evil",
    "file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "ms-settings:",
    "https://user:pass@nodejs.org/", "",
])
def test_open_url_refuses_everything_else(client, browser, url):
    assert client.post("/api/open-url", json={"url": url}, headers=tok()).status_code == 403
    assert browser == []


def test_open_url_allows_a_jobs_own_link_at_a_moment(client, browser):
    jid = jobs.create("transcript", "https://www.youtube.com/watch?v=R6MlUcmOul8")["id"]
    try:
        at = "https://www.youtube.com/watch?v=R6MlUcmOul8&t=23s"
        assert client.post("/api/open-url", json={"url": at}, headers=tok()).status_code == 200
        other = "https://www.youtube.com/watch?v=someoneelse&t=23s"
        assert client.post("/api/open-url", json={"url": other}, headers=tok()).status_code == 403
    finally:
        with jobs._lock:
            jobs._forget(jid)


# ---------------------------------------------------- request-chosen paths

@pytest.fixture
def captured(monkeypatch):
    """Record what a job would run with, without running an engine."""
    seen = []

    def fake(jid, url, options):
        seen.append(dict(options))
        return {}

    monkeypatch.setitem(appmain._RUNNERS, "download", (fake, "work"))
    monkeypatch.setitem(appmain._RUNNERS, "transcript", (fake, "work"))
    monkeypatch.setattr(appmain.transcribe, "run_transcript", fake)
    return seen


def _settle():
    deadline = time.time() + 5
    while jobs._running and time.time() < deadline:
        time.sleep(0.02)


def test_jobs_ignore_request_chosen_folders_and_templates(client, captured):
    r = client.post("/api/jobs", headers=tok(), json={
        "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "kind": "download",
        "options": {"output_dir": "C:/Users/Public/Startup", "output_template": "../../x.bat",
                    "local_path": "C:/Windows/win.ini", "quality": "720"}})
    assert r.status_code == 200
    _settle()
    assert captured and captured[0] == {"quality": "720"}


@pytest.mark.parametrize("value", ["C:\\evil\\curl.exe", "\\\\server\\share\\aria2c.exe",
                                   "curl", "ffmpeg"])
def test_external_downloader_is_only_aria2c(client, value):
    r = client.post("/api/settings", json={"external_downloader": value}, headers=tok())
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "external_downloader"
    assert config.get()["external_downloader"] in ("", "aria2c")


@pytest.mark.parametrize("name", ["x/..\\..\\..\\victim", "..\\victim", "../victim",
                                  "C:\\Windows", "tiny/../../x", "not-a-model", ""])
def test_model_names_must_come_from_the_catalog(client, monkeypatch, tmp_path, name):
    purged = []
    monkeypatch.setattr(appmain.models, "purge", lambda n: purged.append(n))
    r = client.post("/api/models/repair", json={"name": name}, headers=tok())
    assert r.status_code == 400 and purged == []


def test_a_catalog_model_can_be_deleted(client, monkeypatch):
    purged = []
    monkeypatch.setattr(appmain.models, "purge", lambda n: purged.append(n))
    r = client.post("/api/models/repair", json={"name": "tiny"}, headers=tok())
    assert r.status_code == 200 and purged == ["tiny"]


def test_a_crafted_model_in_job_options_is_refused(client, captured):
    r = client.post("/api/jobs", headers=tok(), json={
        "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "kind": "transcript",
        "options": {"model": "x/..\\..\\victim"}})
    assert r.status_code == 400 and captured == []


@pytest.mark.parametrize("filename", ["..\\..\\Startup\\evil.bat", "../../evil.bat",
                                      "C:\\Windows\\evil.bat", "/etc/evil.bat"])
def test_upload_names_cannot_escape_the_upload_folder(client, captured, filename):
    r = client.post("/api/transcribe-file", headers=tok(),
                    files={"file": (filename, b"RIFF....", "audio/wav")},
                    data={"options": '{"output_dir": "C:/Windows", "local_path": "C:/x"}'})
    assert r.status_code == 200
    job = r.json()["jobs"][0]
    _settle()
    local = Path(os.path.abspath(captured[-1]["local_path"]))
    assert Path(os.path.abspath(jobs.UPLOAD_ROOT)) in local.parents
    assert local.name == "evil.bat"
    assert "output_dir" not in captured[-1]
    assert job["title"] == "evil.bat"
    # The job finished, so its temporary copy is gone (T-07).
    assert wait_until(lambda: not local.exists())


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_two_uploads_with_one_name_never_clobber(client, captured):
    for body in (b"first", b"second"):
        r = client.post("/api/transcribe-file", headers=tok(),
                        files={"file": ("speech.wav", body, "audio/wav")})
        assert r.status_code == 200
    _settle()
    paths = [Path(c["local_path"]) for c in captured[-2:]]
    assert paths[0] != paths[1]


def test_upload_title_keeps_the_users_file_name(client, captured):
    r = client.post("/api/transcribe-file", headers=tok(),
                    files={"file": ("Interview: part 1?.m4a", b"x", "audio/mp4")})
    assert r.status_code == 200
    job = r.json()["jobs"][0]
    assert job["title"] == "Interview: part 1?.m4a"
    _settle()
    assert Path(captured[-1]["local_path"]).name == "Interview_ part 1_.m4a"


def test_safe_upload_name():
    assert appmain.safe_upload_name("..\\..\\a.wav") == "a.wav"
    assert appmain.safe_upload_name("") == "upload"
    assert appmain.safe_upload_name("...") == "upload"
    assert appmain.safe_upload_name("CON") == "CON"
    long = appmain.safe_upload_name("x" * 400 + ".wav")
    assert len(long) <= 150 and long.endswith(".wav")


def test_cookie_sign_in_url_cannot_become_a_browser_switch(client, monkeypatch):
    started = []
    monkeypatch.setattr(appmain.cookies, "start_login_browser",
                        lambda root, url: started.append(url) or {"ok": True})
    for url in ("--remote-debugging-port=9222", "file:///C:/", "javascript:x"):
        r = client.post("/api/cookies/login", json={"url": url}, headers=tok())
        assert r.status_code == 400
    assert started == []
