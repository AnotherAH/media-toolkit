"""Regression tests for problems found reviewing the core area.

Each test names the failure it guards against. All offline.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import run
from app import config, cookies, errors, folderpick, jobs
from app import main as appmain

PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setattr(appmain, "DEV_NO_TOKEN", False)
    monkeypatch.setattr(appmain, "REMOTE", False)


@pytest.fixture
def client():
    return TestClient(appmain.app, base_url=BASE)


def tok() -> dict:
    return {"X-MT-Token": appmain.TOKEN}


def _drop(*ids):
    with jobs._lock:
        for jid in ids:
            jobs._forget(jid)


# ------------------------------------------------------------------ history

def test_a_damaged_history_record_cannot_break_the_queue(monkeypatch, tmp_path):
    """One record with a wrong shape used to make every snapshot, and so
    GET /api/jobs and the event stream, raise."""
    path = tmp_path / "history.json"
    monkeypatch.setattr(jobs, "history_path", lambda: path)
    path.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "j9001", "kind": "download", "status": "done", "url": "https://x.test/a",
         "files": "not a list", "steps": [1, 2], "options": ["bad"], "title": 5},
        {"id": "j9002", "kind": "transcript", "status": "error", "url": "",
         "files": [{"name": "no path"}, {"path": "C:/keep.txt", "name": "keep.txt"}],
         "progress": 1, "created": 12, "updated": "later"},
    ]}), encoding="utf-8")
    try:
        assert jobs.load_history() == 2
        snap = {j["id"]: j for j in jobs.all_jobs()}
        a, b = snap["j9001"], snap["j9002"]
        assert a["files"] == [] and a["steps"] == [] and a["options"] == {}
        assert a["title"] == "5" and a["from_history"] is True
        assert [f["path"] for f in b["files"]] == ["C:/keep.txt"]
        assert b["progress"] == 1 and isinstance(b["updated"], float)
        jobs.add_file("j9002", "C:/new.txt")         # used to KeyError on the pathless row
    finally:
        _drop("j9001", "j9002")


def test_history_survives_a_value_json_cannot_write(monkeypatch, tmp_path):
    path = tmp_path / "history.json"
    monkeypatch.setattr(jobs, "history_path", lambda: path)
    jid = jobs.create("download", "https://x.test/p")["id"]
    try:
        jobs._finish(jid, status="done", stage="Done", result={"output_dir": Path("C:/v")})
        jobs.save_history()
        saved = json.loads(path.read_text("utf-8"))["jobs"]
        mine = next(r for r in saved if r["id"] == jid)
        assert mine["result"]["output_dir"] in ("C:\\v", "C:/v")
    finally:
        _drop(jid)


# -------------------------------------------------------------------- hints

def test_hints_are_plain_text_and_the_thumbnail_is_a_web_address():
    job = jobs.create("download", "https://x.test/h", hints={
        "title": "  Big Buck Bunny  ", "thumbnail": "javascript:alert(1)",
        "uploader": "Blender", "status": "done"})
    try:
        assert job["title"] == "Big Buck Bunny" and job["thumbnail"] == ""
        assert job["status"] == "queued"
        ok = jobs.create("download", "https://x.test/i",
                         hints={"thumbnail": "https://i.ytimg.com/vi/a/hq.jpg"})
        assert ok["thumbnail"] == "https://i.ytimg.com/vi/a/hq.jpg"
        _drop(ok["id"])
    finally:
        _drop(job["id"])


# ------------------------------------------------------------------ cookies

def _loads_in_yt_dlp(path: Path) -> int:
    from yt_dlp.cookies import YoutubeDLCookieJar
    jar = YoutubeDLCookieJar(str(path))
    jar.load()
    return len(jar)


def test_a_bad_row_in_a_paste_cannot_break_the_whole_jar(tmp_path):
    """Python's loader rejects the WHOLE file when a row's subdomain flag
    disagrees with its leading dot; one such row used to cost every saved
    sign-in, including the ones merged in earlier."""
    dest = tmp_path / "cookies.txt"
    cookies.import_text("SID=good", dest, site="youtube.com")
    pasted = "\n".join([
        "# Netscape HTTP Cookie File",
        ".instagram.com\tFALSE\t/\tTRUE\t0\tsessionid\tabc",       # flag disagrees
        "www.tiktok.com\tTRUE\t/\tTRUE\t1893456000\tsid_tt\tdef",    # flag disagrees
        ".x.com\tTRUE\t/\tTRUE\tnever\tct0\tghi",                   # bad expiry: dropped
    ])
    result = cookies.import_text(pasted, dest)
    assert result["cookies"] == 2
    assert _loads_in_yt_dlp(dest) == 3


def test_a_devtools_table_is_not_taken_for_a_cookies_file(tmp_path):
    table = "SID\tabc\t.google.com\t/\t2027-01-01T00:00:00.000Z\t35\t✓\t✓\tLax\t\tMedium"
    with pytest.raises(ValueError):
        cookies.import_text(table, tmp_path / "c.txt")


def test_a_json_export_is_not_taken_for_a_cookie_header(tmp_path):
    exported = '[{"domain":".youtube.com","name":"SID","value":"abc=="}]'
    with pytest.raises(ValueError):
        cookies.import_text(exported, tmp_path / "c.txt", site="youtube.com")


def test_harvest_skips_values_that_would_split_a_row(tmp_path, monkeypatch):
    monkeypatch.setattr(cookies, "_devtools_endpoint", lambda profile, timeout=2.0: "ws://x")
    monkeypatch.setattr(cookies, "_close_login_browser", lambda profile: True)
    monkeypatch.setattr(cookies, "_cdp", lambda ws, method, timeout=10.0: {"cookies": [
        {"domain": ".youtube.com", "name": "SID", "value": "ok", "path": "/", "expires": -1},
        {"domain": ".youtube.com", "name": "BAD", "value": "a\tb", "path": "/"},
        {"domain": "www.youtube.com", "name": "PREF", "value": "f1", "expires": 1893456000.5},
    ]})
    dest = tmp_path / "cookies.txt"
    result = cookies.harvest_login_cookies(dest, tmp_path / "profile")
    assert result["ok"] and result["cookies"] == 2
    assert _loads_in_yt_dlp(dest) == 2


# ------------------------------------------------------------ folder picker

def test_a_picker_left_open_too_long_does_not_open_a_second_one(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args[0])
        raise subprocess.TimeoutExpired(args, 600)

    monkeypatch.setattr(folderpick.subprocess, "run", fake_run)
    assert folderpick._run_ps("script", "C:/") == ""
    assert calls == ["powershell.exe"]


# ----------------------------------------------------------------- security

def test_port_80_host_without_a_port_is_our_own_address():
    scope = {"path": "/api/jobs", "method": "GET", "server": ("127.0.0.1", 80)}
    assert appmain.check_request(scope, {"host": "127.0.0.1"}) is None
    assert appmain.check_request(scope, {"host": "localhost", "origin": "http://localhost"}) is None
    assert appmain.check_request(scope, {"host": "evil.example"}) is not None
    other = {"path": "/api/jobs", "method": "GET", "server": ("127.0.0.1", 8931)}
    assert appmain.check_request(other, {"host": "127.0.0.1"}) is not None


def test_show_in_folder_always_quotes_the_path(client, monkeypatch):
    """Explorer splits /select, at commas; an unquoted 'a,b.mp4' opened
    Documents instead of selecting the file."""
    launched = []
    monkeypatch.setattr(appmain.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    folder = Path(config.get()["download_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "a,b.mp4"
    f.write_bytes(b"\x00")
    try:
        r = client.post("/api/reveal", json={"path": str(f)}, headers=tok())
        assert r.status_code == 200
        if os.name == "nt":
            assert launched == [f'explorer.exe /select,"{f.resolve()}"']
    finally:
        f.unlink()


def test_open_no_longer_hands_spreadsheets_to_the_shell(client, monkeypatch):
    monkeypatch.setattr(appmain, "_shell_open", lambda target: None)
    folder = Path(config.get()["download_dir"])
    f = folder / "list.csv"
    f.write_text("=1+1", encoding="utf-8")
    try:
        assert client.post("/api/open", json={"path": str(f)}, headers=tok()).status_code == 403
    finally:
        f.unlink()


# ----------------------------------------------------------------- settings

def test_an_unchanged_unreachable_folder_does_not_block_other_settings(client, monkeypatch):
    """A page that saves every field at once resends the folders. With the
    download drive unplugged, no other setting could be saved."""
    monkeypatch.setattr(appmain, "check_folder", lambda path: "Can't use this folder: gone")
    cfg = config.get()
    r = client.post("/api/settings", headers=tok(), json={
        "download_dir": cfg["download_dir"], "transcript_dir": cfg["transcript_dir"],
        "rate_limit": "5M"})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["rate_limit"] == "5M"
    r = client.post("/api/settings", headers=tok(), json={"download_dir": "D:/somewhere/new"})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "download_dir"
    # First-run setup proves every folder, changed or not.
    r = client.post("/api/setup", headers=tok(), json={
        "download_dir": cfg["download_dir"], "transcript_dir": cfg["transcript_dir"]})
    assert r.status_code == 400
    client.post("/api/settings", headers=tok(), json={"rate_limit": ""})


@pytest.mark.parametrize("hours,ok", [(3, True), (1.5, True), (168, True), (0, False),
                                      (-2, False), (1000, False)])
def test_wait_hours_are_bounded(client, hours, ok):
    r = client.post("/api/settings", headers=tok(), json={"lv_wait_hours": hours})
    assert (r.status_code == 200) is ok, r.text
    client.post("/api/settings", headers=tok(), json={"lv_wait_hours": 3})


def test_bootstrap_does_not_grow_path_each_time(monkeypatch):
    config.bootstrap()
    before = os.environ["PATH"]
    config.bootstrap()
    config.bootstrap()
    assert os.environ["PATH"] == before


# ------------------------------------------------------------------- errors

def test_terminated_alone_is_not_a_removed_video():
    exc = RuntimeError("ffmpeg was terminated while writing the file")
    assert errors.classify(exc, "https://youtu.be/x", "download")["code"] != "unavailable"
    gone = RuntimeError("ERROR: [youtube] x: This account has been terminated for a violation")
    assert errors.classify(gone, "https://youtu.be/x", "download")["code"] == "unavailable"


@pytest.mark.parametrize("text", [
    "ffmpeg -cookies 'SID=secret; path=/;' -i https://a.test/x",
    "['ffmpeg', '-cookies', 'SID=secret; path=/; domain=.youtube.com;\\r\\n', '-i']",
    'ffmpeg -cookies "SID=secret" -i x',
])
def test_ffmpeg_cookie_arguments_never_reach_the_log(text):
    assert "secret" not in errors.redact(text)


# ------------------------------------------------------------------ launcher

@pytest.mark.parametrize("host,expect", [
    ("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
    ("::", "[::1]"), ("::1", "[::1]"), ("192.168.1.5", "192.168.1.5"),
    ("fe80::1", "[fe80::1]"), ("", "127.0.0.1")])
def test_second_launch_reaches_the_address_the_instance_listens_on(host, expect):
    assert run._connect_host({"host": host}) == expect


def test_instance_file_records_the_listening_address(tmp_path):
    run._write_instance(tmp_path, 8931, "tok", remote=True, host="192.168.1.5")
    info = json.loads((tmp_path / "instance.json").read_text("utf-8"))
    assert info["host"] == "192.168.1.5" and info["remote"] is True


def test_reattaching_holds_the_instance_open_first(monkeypatch):
    """A background instance quits when its last job ends; the window the
    second launch opens needs a heartbeat's grace to get there first."""
    order = []
    monkeypatch.setattr(run, "find_running", lambda root, wait=0: {
        "port": 8931, "pid": 1, "token": "t", "host": "127.0.0.1"})
    monkeypatch.setattr(run, "hold_instance", lambda info: order.append("hold") or True)
    monkeypatch.setattr(run, "open_ui", lambda url, browser, root: order.append("open"))
    monkeypatch.setattr(run, "_setup_log", lambda root, console: root / "app.log")
    monkeypatch.setattr(run, "_prepare_runtime", lambda: None)

    class Busy:
        def __init__(self, root):
            pass

        def acquire(self):
            return False

    monkeypatch.setattr(run, "InstanceLock", Busy)
    assert run.main([]) == 0
    assert order == ["hold", "open"]


def test_hold_instance_gives_up_quietly_when_nothing_listens():
    assert run.hold_instance({"port": 9, "token": "t", "host": "127.0.0.1"}) is False


def test_a_portable_copy_recommends_folders_on_its_own_drive(client, monkeypatch):
    monkeypatch.setattr(config, "PORTABLE", True)
    s = client.get("/api/setup").json()["suggestions"]
    assert s["portable"] is True
    assert s["download_dir"] == str(config.DATA_ROOT / "downloads")
    assert s["transcript_dir"] == str(config.DATA_ROOT / "transcripts")
    monkeypatch.setattr(config, "PORTABLE", False)
    s = client.get("/api/setup").json()["suggestions"]
    assert s["portable"] is False and s["download_dir"].endswith("Media Toolkit")
