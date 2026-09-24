"""API behaviour (app/main.py): links, settings, setup, previews, the queue,
transcripts and the site list. Offline: engines are replaced by fakes."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, errors, jobs
from app import main as appmain

BASE = "http://127.0.0.1:8931"


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setattr(appmain, "DEV_NO_TOKEN", False)
    monkeypatch.setattr(appmain, "REMOTE", False)


@pytest.fixture
def client():
    c = TestClient(appmain.app, base_url=BASE)
    c.headers["X-MT-Token"] = appmain.TOKEN
    return c


@pytest.fixture
def restore_config():
    """Settings tests change config.json; put it back afterwards."""
    before = config.get()
    yield
    config.save(before)


@pytest.fixture
def fake_hw(monkeypatch):
    hw = {"gpus": [], "nvidia_name": "NVIDIA GeForce RTX 5080", "gpu_ready": False,
          "gpu_pack_size_mb": 740, "cuda": False, "vram_mb": 16000, "cpu_threads": 32,
          "recommended_model": "small", "note": "", "ffmpeg": True}
    monkeypatch.setattr(appmain.hardware, "summary", lambda: dict(hw))
    return hw


def _settle():
    deadline = time.time() + 5
    while jobs._running and time.time() < deadline:
        time.sleep(0.02)


def _forget(*ids):
    with jobs._lock:
        for jid in ids:
            jobs._forget(jid)


@pytest.fixture
def runner(monkeypatch):
    """A fake engine that records its calls and can be told how to end."""
    calls = []
    behaviour = {"raise": None, "result": {}}

    def fake(jid, url, options):
        calls.append((jid, url, dict(options)))
        if behaviour["raise"] is not None:
            raise behaviour["raise"]
        return behaviour["result"]

    for kind in ("download", "transcript"):
        monkeypatch.setitem(appmain._RUNNERS, kind, (fake, "work"))
    monkeypatch.setitem(appmain._RUNNERS, "live", (fake, "live"))
    jobs.register("download", fake)
    yield calls, behaviour
    _settle()
    from app import live, media, transcribe
    jobs.register("download", media.run_download)
    jobs.register("transcript", transcribe.run_transcript)
    jobs.register("live", live.run_live, pool="live")


# ------------------------------------------------------------------ links

def test_split_links_whitespace_only():
    links, ignored = appmain.split_links("https://a.test/x https://b.test/y\n\thttps://a.test/x")
    assert links == ["https://a.test/x", "https://b.test/y"] and ignored == 0
    links, ignored = appmain.split_links("hello world")
    assert links == [] and ignored == 2
    links, _ = appmain.split_links("https://example.invalid/video?ids=1,2,3")
    assert links == ["https://example.invalid/video?ids=1,2,3"]
    links, ignored = appmain.split_links("ftp://x.test/a file:///C:/x javascript:alert(1) "
                                         "https://ok.test/")
    assert links == ["https://ok.test/"] and ignored == 3


def test_text_that_is_not_a_link_creates_no_job(client, runner):
    calls, _ = runner
    before = len(jobs.all_jobs())
    r = client.post("/api/jobs", json={"url": "hello world", "kind": "download"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "bad_link" and detail["ignored"] == 2 and detail["title"]
    assert len(jobs.all_jobs()) == before and calls == []


def test_one_job_per_link_with_preview_hints(client, runner):
    calls, _ = runner
    url1 = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    url2 = "https://www.youtube.com/watch?v=eRsGyueVLvQ"
    r = client.post("/api/jobs", json={
        "url": f"{url1} not-a-link {url2}", "kind": "download",
        "hints": {url1: {"title": "Big Buck Bunny", "thumbnail": "https://i.ytimg.com/a.jpg",
                         "uploader": "Blender"}}})
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == 1 and len(body["jobs"]) == 2
    first = body["jobs"][0]
    assert first["title"] == "Big Buck Bunny" and first["uploader"] == "Blender"
    assert body["jobs"][1]["title"] == url2
    _settle()
    assert sorted(c[1] for c in calls) == sorted([url1, url2])
    _forget(*(j["id"] for j in body["jobs"]))


def test_unknown_kind_is_refused(client, runner):
    r = client.post("/api/jobs", json={"url": "https://a.test/", "kind": "exec"})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "kind"


# --------------------------------------------------------------- settings

def test_settings_returns_defaults(client):
    body = client.get("/api/settings").json()
    assert body["defaults"]["dl_quality"] == "1080"
    assert body["defaults"]["set_mtime"] is False
    assert "folder_problems" in body and "config" in body


def test_invalid_proxy_is_refused_and_nothing_saved(client, restore_config):
    before = config.get()["proxy"]
    r = client.post("/api/settings", json={"proxy": "127.0.0.1:8080"})
    assert r.status_code == 400
    assert r.json()["detail"] == {"field": "proxy",
                                  "message": "Start with http://, https://, socks4:// or socks5://"}
    assert config.get()["proxy"] == before
    assert client.post("/api/settings", json={"proxy": "socks5h://127.0.0.1:1080"}).status_code == 200
    assert client.post("/api/settings", json={"proxy": ""}).status_code == 200


def test_folder_is_proven_before_it_is_saved(client, restore_config, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    before = config.get()["download_dir"]
    r = client.post("/api/settings", json={"download_dir": str(blocker / "sub")})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "download_dir"
    assert r.json()["detail"]["message"].startswith("Can't use this folder")
    r = client.post("/api/settings", json={"download_dir": "relative\\folder"})
    assert r.status_code == 400
    assert config.get()["download_dir"] == before

    good = tmp_path / "Videos" / "Media Toolkit"
    r = client.post("/api/settings", json={"download_dir": str(good)})
    assert r.status_code == 200 and r.json()["config"]["download_dir"] == str(good)
    assert good.is_dir() and not list(good.iterdir())          # write test cleaned up


def test_settings_type_checks(client, restore_config):
    assert client.post("/api/settings", json={"concurrent_fragments": 64}).status_code == 400
    assert client.post("/api/settings", json={"concurrent_fragments": "abc"}).status_code == 400
    assert client.post("/api/settings", json={"sleep_requests": -1}).status_code == 400
    assert client.post("/api/settings", json={"geo_bypass_country": "USA"}).status_code == 400
    assert client.post("/api/settings", json={"rate_limit": "fast"}).status_code == 400
    assert client.post("/api/settings", json={"whisper_model": "..\\x"}).status_code == 400
    r = client.post("/api/settings", json={"geo_bypass_country": "us", "concurrent_fragments": "4",
                                           "embed_metadata": "false", "unknown_key": 1})
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["geo_bypass_country"] == "US" and cfg["concurrent_fragments"] == 4
    assert cfg["embed_metadata"] is False and "unknown_key" not in cfg


def test_filename_presets_map_to_templates(client, restore_config):
    r = client.post("/api/settings", json={"filename_preset": "title"})
    assert r.json()["config"]["output_template"] == "%(title).200B.%(ext)s"
    r = client.post("/api/settings", json={"filename_preset": "channel_title"})
    assert r.json()["config"]["output_template"] == \
        "%(uploader,channel|Unknown).60B - %(title).150B.%(ext)s"
    r = client.post("/api/settings", json={"filename_preset": "date_title"})
    assert r.json()["config"]["output_template"] == \
        "%(upload_date>%Y-%m-%d|)s %(title).180B.%(ext)s"
    r = client.post("/api/settings", json={"output_template": "%(id)s.%(ext)s"})
    assert r.json()["config"]["filename_preset"] == "custom"
    r = client.post("/api/settings", json={"filename_preset": "custom",
                                           "output_template": "%(title)s.%(ext)s"})
    assert r.json()["config"]["output_template"] == "%(title)s.%(ext)s"
    for bad in ("..\\..\\Startup\\%(id)s.bat", "C:\\x\\%(id)s", "\\\\srv\\s\\%(id)s"):
        assert client.post("/api/settings", json={"output_template": bad}).status_code == 400
    r = client.post("/api/settings", json={"filename_preset": "title_id"})
    assert r.json()["config"]["output_template"] == "%(title).180B [%(id)s].%(ext)s"


def test_one_sign_in_source_at_a_time(client, restore_config, tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    r = client.post("/api/settings", json={"cookies_file": str(jar)})
    assert r.status_code == 200
    r = client.post("/api/settings", json={"cookies_browser": "firefox"})
    cfg = r.json()["config"]
    assert cfg["cookies_browser"] == "firefox" and cfg["cookies_file"] == ""
    r = client.post("/api/settings", json={"cookies_file": str(jar)})
    cfg = r.json()["config"]
    assert cfg["cookies_file"] == str(jar) and cfg["cookies_browser"] == ""
    assert client.post("/api/settings", json={"cookies_file": str(tmp_path / "none.txt")}) \
        .status_code == 400
    assert client.post("/api/settings", json={"cookies_browser": "netscape"}).status_code == 400


def test_pasted_cookies_need_a_site_and_clear_the_browser(client, restore_config, monkeypatch,
                                                          tmp_path):
    monkeypatch.setattr(appmain, "_cookie_jar", lambda: tmp_path / "cookies.txt")
    config.save({"cookies_browser": "edge"})
    r = client.post("/api/cookies/import", json={"text": "SID=abc; HSID=def"})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "site"
    r = client.post("/api/cookies/import", json={"text": "SID=abc; HSID=def", "site": "YouTube"})
    assert r.status_code == 200 and r.json()["domain"] == ".youtube.com"
    cfg = config.get()
    assert cfg["cookies_browser"] == "" and cfg["cookies_file"] == str(tmp_path / "cookies.txt")


def test_sign_out_forgets_everything(client, restore_config, monkeypatch, tmp_path):
    forgot = []
    monkeypatch.setattr(appmain.cookies, "forget",
                        lambda root, jar: forgot.append((root, jar)) or {"removed": [],
                                                                         "profile_left": False})
    config.save({"cookies_browser": "edge", "cookies_profile": "Default"})
    r = client.post("/api/cookies/clear")
    assert r.status_code == 200 and forgot
    cfg = config.get()
    assert cfg["cookies_browser"] == "" and cfg["cookies_profile"] == "" and cfg["cookies_file"] == ""


# ------------------------------------------------------------------ setup

def test_setup_suggestions(client, fake_hw):
    body = client.get("/api/setup").json()
    s = body["suggestions"]
    assert s["home"] == str(Path.home())
    assert s["download_dir"].endswith(os.path.join("", "Media Toolkit"))
    assert s["transcript_dir"].endswith("Transcripts")
    assert s["nvidia_name"] == "NVIDIA GeForce RTX 5080" and s["gpu_ready"] is False
    assert s["gpu_pack_size_mb"] == 740 and s["whisper_model"] == "small"


def test_setup_never_saves_an_unusable_folder(client, restore_config, tmp_path):
    config.save({"setup_complete": False})
    blocker = tmp_path / "file"
    blocker.write_text("x")
    r = client.post("/api/setup", json={"download_dir": str(blocker / "x"),
                                        "transcript_dir": str(tmp_path / "t")})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "download_dir"
    cfg = config.get()
    assert cfg["setup_complete"] is False and cfg["download_dir"] != str(blocker / "x")
    r = client.post("/api/setup", json={"download_dir": "", "transcript_dir": str(tmp_path)})
    assert r.status_code == 400
    assert r.json()["detail"]["message"] == "Choose a folder for both downloads and transcripts."
    r = client.post("/api/setup", json={"download_dir": str(tmp_path / "d"),
                                        "transcript_dir": str(tmp_path / "t"),
                                        "whisper_model": "small", "set_mtime": False})
    assert r.status_code == 200 and config.get()["setup_complete"] is True


# ---------------------------------------------------------------- previews

def test_probe_errors_are_structured(client, monkeypatch):
    def boom(url):
        raise RuntimeError("ERROR: [generic] Unsupported URL: https://example.com/")
    monkeypatch.setattr(appmain.media, "probe", boom)
    r = client.get("/api/probe", params={"url": "https://example.com/"})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "bad_link" and d["title"] and d["body"] and d["actions"]
    assert not d["detail"].startswith("ERROR")

    def odd(url):
        raise RuntimeError("something nobody expected")
    monkeypatch.setattr(appmain.media, "probe", odd)
    d = client.get("/api/probe", params={"url": "https://example.com/"}).json()["detail"]
    assert d["title"] == "Couldn't read this link"


def test_preview_refuses_non_web_links(client, monkeypatch):
    called = []
    monkeypatch.setattr(appmain.media, "probe", lambda url: called.append(url))
    for url in ("file:///C:/Windows/win.ini", "C:\\x", "ftp://a/b", ""):
        r = client.get("/api/probe", params={"url": url})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_link"
    assert called == []


def test_live_check_offline_is_an_answer(client, monkeypatch):
    from app import live

    def offline(url, quality, audio_only):
        raise live.NotLiveYet("This live event will begin in 3 hours.",
                              {"uploader": "NASA", "title": "Launch", "release_timestamp": 1.9e9},
                              upcoming=True)
    monkeypatch.setattr(live, "check", None, raising=False)
    monkeypatch.setattr(live, "resolve", lambda url, q="best", a=False: offline(url, q, a))
    r = client.get("/api/live-check", params={"url": "https://www.youtube.com/@NASA/live"})
    assert r.status_code == 200
    body = r.json()
    assert body["offline"] is True and body["upcoming"] is True and body["uploader"] == "NASA"
    assert body["release_timestamp"] == 1.9e9 and body["is_live"] is False


def test_live_check_passes_the_chosen_quality(client, monkeypatch):
    from app import live
    seen = []
    monkeypatch.setattr(live, "check", lambda url, q, a: seen.append((q, a)) or {"is_live": True})
    client.get("/api/live-check", params={"url": "https://twitch.tv/x", "quality": "480"})
    client.get("/api/live-check", params={"url": "https://twitch.tv/x", "quality": "evil",
                                          "audio_only": "true"})
    assert seen == [("480", False), ("best", True)]


def test_live_check_real_failure_is_structured(client, monkeypatch):
    from app import live

    def private(url, q, a):
        raise RuntimeError("ERROR: [youtube] abc: Private video. Sign in if you've been granted "
                           "access to this video")
    monkeypatch.setattr(live, "check", private)
    r = client.get("/api/live-check", params={"url": "https://www.youtube.com/watch?v=abc"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "signin"


def test_sites_are_grouped_and_empty_until_typed(client):
    empty = client.get("/api/sites").json()
    assert empty["matches"] == [] and empty["total"] > 500
    insta = client.get("/api/sites", params={"q": "insta"}).json()["matches"]
    assert insta.count("Instagram") == 1
    assert not any(":" in m for m in insta)
    yt = client.get("/api/sites", params={"q": "youtube"}).json()["matches"]
    assert yt[0] == "YouTube" and yt.count("YouTube") == 1


def test_about(client):
    body = client.get("/api/about").json()
    from app import __version__
    assert body["version"] == __version__
    for key in ("yt_dlp", "python", "frozen", "data_dir", "log_path", "notices_path"):
        assert key in body
    assert client.get("/api/openapi.json").json()["info"]["version"] == __version__


def test_update_endpoints_use_the_updater(client, monkeypatch):
    from app import updater
    monkeypatch.setattr(updater, "update", lambda: {"ok": True, "version": "2099.1.1",
                                                    "message": "Updated", "restart_required": True})
    monkeypatch.setattr(updater, "check_app_update",
                        lambda: {"current": "1.2.0", "latest": "1.3.0", "update_available": True,
                                 "url": "https://github.com/AnotherAH/media-toolkit/releases"})
    assert client.post("/api/update-ytdlp").json()["restart_required"] is True
    assert client.get("/api/update-check").json()["latest"] == "1.3.0"


# ------------------------------------------------------------------- queue

def test_retry_remove_clear_and_restore(client, runner):
    calls, behaviour = runner
    behaviour["raise"] = RuntimeError("HTTP Error 429: Too Many Requests")
    jid = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=x",
                                         "kind": "download"}).json()["jobs"][0]["id"]
    _settle()
    job = client.get(f"/api/jobs/{jid}").json()
    assert job["status"] == "error" and job["error"]["code"] == "rate_limited"
    assert job["message"].startswith("YouTube is limiting downloads")

    behaviour["raise"] = None
    r = client.post(f"/api/jobs/{jid}/retry", json={"options": {"output_dir": "C:/x",
                                                                 "quality": "480"}})
    assert r.status_code == 200 and r.json()["job"]["id"] == jid
    _settle()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "done"
    assert calls[-1][2] == {"quality": "480"}

    assert client.post("/api/jobs/clear-completed").json()["removed"] >= 1
    assert client.get(f"/api/jobs/{jid}").status_code == 404
    assert client.post("/api/jobs/restore").json()["restored"] >= 1
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "done"
    assert client.delete(f"/api/jobs/{jid}").json() == {"removed": True}
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_clear_completed_keeps_failures(client, runner):
    _, behaviour = runner
    behaviour["raise"] = RuntimeError("Video unavailable")
    jid = client.post("/api/jobs", json={"url": "https://a.test/v", "kind": "download"}) \
        .json()["jobs"][0]["id"]
    _settle()
    client.post("/api/jobs/clear-completed")
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "error"
    _forget(jid)


def test_retry_of_a_lost_upload_says_so(client, runner):
    job = jobs.create("transcript", "", {"local_path": "C:/nowhere/gone.wav"}, title="gone.wav")
    jobs._finish(job["id"], status="error", stage="Failed")
    r = client.post(f"/api/jobs/{job['id']}/retry")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unreadable_file"
    _forget(job["id"])


def test_jobs_and_events_are_slim(client):
    job = jobs.create("transcript", "https://a.test/v")
    jobs._finish(job["id"], status="done", stage="Done",
                 result={"segments": [{"start": 0, "end": 1, "text": "x" * 5000}],
                         "text": "x" * 5000, "stats": {"words": 1}})
    listed = client.get("/api/jobs").json()["jobs"]
    mine = next(j for j in listed if j["id"] == job["id"])
    assert "segments" not in mine["result"] and "text" not in mine["result"]
    assert mine["result"]["stats"] == {"words": 1}
    _forget(job["id"])


# ------------------------------------------------------------ transcripts

@pytest.fixture
def transcript(tmp_path, restore_config):
    folder = tmp_path / "Transcripts"
    folder.mkdir()
    config.save({"transcript_dir": str(folder)})
    stem = "Tears of Steel"
    segs = [{"start": 0.0, "end": 2.5, "text": "Hello there."},
            {"start": 23.0, "end": 25.0, "text": "General Kenobi."}]
    meta = {"title": stem, "url": "https://www.youtube.com/watch?v=R6MlUcmOul8",
            "uploader": "Blender", "duration": 734}
    side = {"version": 1, "stem": stem, "created": 1_700_000_000.0, "formats": ["txt"],
            "meta": meta, "detail": {"source": "official"},
            "stats": {"words": 4, "tokens": 6}, "segments": segs}
    (folder / f"{stem}.mt.json").write_text(json.dumps(side), encoding="utf-8")
    (folder / f"{stem}.txt").write_text("Hello there. General Kenobi.", encoding="utf-8")
    job = jobs.create("transcript", meta["url"], {}, title=stem)
    jobs._finish(job["id"], status="done", stage="Done",
                 result={"meta": meta, "output_dir": str(folder), "stem": stem,
                         "segments": segs, "text": "Hello there. General Kenobi."})
    yield folder, stem, job["id"]
    _forget(job["id"])


def test_recent_transcripts_from_sidecars(client, transcript):
    folder, stem, _ = transcript
    body = client.get("/api/transcripts", params={"limit": 8}).json()
    row = body["transcripts"][0]
    assert row["stem"] == stem and row["title"] == stem and row["site"] == "YouTube"
    assert row["words"] == 4 and row["source"] == "official" and row["date"] == 1_700_000_000.0
    assert row["path"] == str(folder / f"{stem}.txt")
    txt = client.get(f"/api/transcripts/{stem}", params={"format": "txt"})
    assert txt.status_code == 200 and "General Kenobi." in txt.text
    js = client.get(f"/api/transcripts/{stem}", params={"format": "json"})
    assert js.status_code == 200 and "Kenobi" in js.text


@pytest.mark.parametrize("stem", ["..", "..\\..\\x", "missing", "a/b"])
def test_transcript_stem_cannot_leave_the_folder(client, transcript, stem):
    assert client.get(f"/api/transcripts/{stem}").status_code == 404


def test_export_writes_a_new_format_once(client, transcript):
    folder, stem, jid = transcript
    r = client.post(f"/api/jobs/{jid}/export", json={"format": "vtt"})
    assert r.status_code == 200 and r.json()["created"] is True
    path = Path(r.json()["path"])
    assert path == folder / f"{stem}.vtt" and path.read_text("utf-8").startswith("WEBVTT")
    again = client.post(f"/api/jobs/{jid}/export", json={"format": "vtt"}).json()
    assert again["created"] is False
    assert client.post(f"/api/jobs/{jid}/export", json={"format": "exe"}).status_code == 400
    assert any(f["path"] == str(path) for f in jobs.get(jid)["files"])


def test_history_job_transcript_comes_from_the_sidecar(client, transcript):
    _, _, jid = transcript
    with jobs._lock:                                 # as if restored from history.json
        jobs._jobs[jid]["result"] = jobs._slim_result(jobs._jobs[jid]["result"])
    r = client.get(f"/api/jobs/{jid}/transcript", params={"format": "srt"})
    assert r.status_code == 200 and "00:00:23,000" in r.text


def test_save_text_rewrites_the_txt(client, transcript):
    folder, stem, jid = transcript
    r = client.post(f"/api/jobs/{jid}/save-text", json={"text": "Edited."})
    assert r.status_code == 200
    assert (folder / f"{stem}.txt").read_text("utf-8") == "Edited."


# --------------------------------------------------------------- models

def test_models_download_and_busy_delete(client, monkeypatch):
    monkeypatch.setattr(appmain.models, "start_download",
                        lambda name: {"name": name, "busy": True})
    assert client.post("/api/models/download", json={"name": "base"}).json()["busy"] is True
    assert client.post("/api/models/download", json={"name": "..\\x"}).status_code == 400

    def busy(name):
        raise RuntimeError("That speech model is downloading right now.")
    monkeypatch.setattr(appmain.models, "purge", busy)
    r = client.post("/api/models/delete", json={"name": "base"})
    assert r.status_code == 409


# ----------------------------------------------------------- temp cleanup

def test_cleanup_temp_removes_only_old_leftovers(tmp_path):
    old, new = tmp_path / "old.part", tmp_path / "new.part"
    old.write_text("x")
    new.write_text("y")
    empty_old, empty_new = tmp_path / "gone", tmp_path / "fresh"
    empty_old.mkdir()
    empty_new.mkdir()
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    os.utime(empty_old, (long_ago, long_ago))
    assert appmain.cleanup_temp(root=tmp_path) == 1
    assert not old.exists() and new.exists()
    assert not empty_old.exists() and empty_new.exists()


def test_error_catalog_copy_has_no_em_dashes():
    for title, body, _ in errors.CATALOG.values():
        assert "\u2014" not in title + body
