"""Cross-module fixes made while integrating 1.2.0: requests one engine made
of a file another engine owned. All offline."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import run
from app import errors, jobs, live, media
from app import main as appmain

BASE = "http://127.0.0.1:8950"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(appmain, "DEV_NO_TOKEN", False)
    monkeypatch.setattr(appmain, "REMOTE", False)
    return TestClient(appmain.app, base_url=BASE, headers={"X-MT-Token": appmain.TOKEN})


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- jobs

def test_stopping_a_live_job_before_it_records_does_not_say_saving():
    """Stop during 'Checking the stream': nothing to save, so no 'Saving'."""
    gate = threading.Event()

    def check(jid, url, o):
        jobs.update(jid, stage="Checking the stream")
        gate.wait(5)
        jobs.raise_if_cancelled(jid)

    jid = jobs.create("live", "https://x.test/live")["id"]
    jobs.submit(jid, check, "https://x.test/live", {}, pool="live")
    assert _wait(lambda: jobs.get(jid)["stage"] == "Checking the stream")
    assert jobs.cancel(jid)
    job = jobs.get(jid)
    assert job["status"] == "stopping" and job["stage"] == "Stopping"
    assert job["live_phase"] is None and not job["indeterminate"]
    gate.set()
    assert _wait(lambda: jobs.get(jid)["status"] == "cancelled")
    jobs.remove(jid)


def test_an_expected_app_error_logs_one_line_not_a_stack_trace(capsys):
    def not_live(jid, url, o):
        jobs.update(jid, live_phase="waiting", next_check_at=time.time() + 60,
                    give_up_at=time.time() + 1)
        raise errors.AppError("live_not_live", "offline channel", Channel="Blender")

    jid = jobs.create("live", "https://x.test/@b/live")["id"]
    jobs.submit(jid, not_live, "https://x.test/@b/live", {}, pool="live")
    assert _wait(lambda: jobs.get(jid)["status"] == "error")
    time.sleep(0.05)
    out = capsys.readouterr()
    text = out.out + out.err
    assert "Traceback" not in text and "live_not_live" in text
    job = jobs.get(jid)
    assert job["next_check_at"] is None and job["give_up_at"] and job["live_phase"] is None
    jobs.remove(jid)


def test_an_unexpected_error_still_logs_its_stack_trace(capsys):
    def broken(jid, url, o):
        raise ValueError("boom")

    jid = jobs.create("download", "https://x.test/v")["id"]
    jobs.submit(jid, broken, "https://x.test/v", {})
    assert _wait(lambda: jobs.get(jid)["status"] == "error")
    time.sleep(0.05)
    out = capsys.readouterr()
    assert "Traceback" in out.out + out.err
    jobs.remove(jid)


# -------------------------------------------------------------- errors

@pytest.mark.parametrize("text, code", [
    ("ERROR: Postprocessing: Cannot cut video since the real and expected durations mismatch. "
     "Different chapters may have already been removed", "convert_failed"),
    ("ERROR: Postprocessing: Conversion failed! C:\\Users\\me\\private notes\\x.mp4", "convert_failed"),
    ("ERROR: Postprocessing: Unable to communicate with SponsorBlock API: timed out", "network"),
    ("ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install", "ffmpeg_missing"),
    ("ERROR: Postprocessing: [Errno 28] No space left on device", "disk_full"),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed", "unavailable"),
])
def test_post_processing_failures_are_conversion_failures(text, code):
    assert errors.classify(Exception(text), "https://www.youtube.com/watch?v=abc", "download")["code"] == code


def test_a_wait_that_ran_out_says_when_it_stopped():
    at = time.mktime((2026, 9, 24, 22, 0, 0, 0, 0, -1))
    err = errors.classify(errors.AppError("live_not_live", "gave up", Channel="NASA",
                                          gave_up_at=at, live_status="is_upcoming"),
                          "https://www.youtube.com/@NASA/live", "live")
    assert err["title"] == "NASA isn't live right now"
    assert err["body"] == "Stopped waiting at 22:00. It hadn't started by then."
    assert err["actions"] == ["retry_wait", "remove"]
    assert err["params"]["gave_up_at"] == at
    assert "\u2014" not in err["body"]


def test_live_not_live_without_waiting_keeps_the_catalog_copy():
    err = errors.classify(errors.AppError("live_not_live", "offline", Channel="NASA"),
                          "https://www.youtube.com/@NASA/live", "live")
    assert err["body"].startswith("Turn on")
    assert err["actions"] == ["retry_wait"]


def test_unreadable_sound_from_a_link_offers_no_file_chooser():
    exc = Exception("Invalid data found when processing input")
    link = errors.classify(exc, "https://www.youtube.com/watch?v=abc", "transcript")
    upload = errors.classify(exc, "", "transcript")
    assert link["code"] == upload["code"] == "unreadable_file"
    assert "choose_file" not in link["actions"] and "retry" in link["actions"]
    assert upload["actions"] == ["choose_file"]
    assert upload["title"] == "This file couldn't be read"
    download = errors.classify(exc, "https://www.youtube.com/watch?v=abc", "download")
    assert download["title"] == "This file couldn't be read"
    assert download["actions"] == ["retry", "copy_details"]


# ------------------------------------------------------------ settings

def test_sponsorblock_categories_are_checked_and_normalised():
    clean = appmain.validate_settings({"sponsorblock_categories": ["sponsor", "intro", "sponsor"]})
    assert clean["sponsorblock_categories"] == "sponsor,intro"
    clean = appmain.validate_settings({"sponsorblock_categories": " Sponsor , outro "})
    assert clean["sponsorblock_categories"] == "sponsor,outro"
    for bad in ("sponsor,nonsense", "", []):
        with pytest.raises(appmain.FieldError) as info:
            appmain.validate_settings({"sponsorblock_categories": bad})
        assert info.value.field == "sponsorblock_categories"


def test_sponsorblock_default_is_one_media_understands():
    from app import config
    cats = config.DEFAULTS["sponsorblock_categories"].split(",")
    assert cats == media.DEFAULT_SPONSOR
    assert all(c in media.SPONSOR_CATEGORIES for c in cats)


def test_capabilities_report_the_runtime_yt_dlp_really_uses(client, monkeypatch):
    monkeypatch.setattr(media, "js_runtime_name", lambda: "quickjs")
    monkeypatch.setattr(media, "impersonate_targets", lambda: ["chrome"])
    monkeypatch.setattr(appmain.ffmpegtools, "encoder_catalog", lambda: [])
    body = client.get("/api/capabilities").json()
    assert body["js_runtime"] == "quickjs"
    assert body["sponsor_labels"]["sponsor"] == "Sponsors"
    assert set(body["quality_labels"]) == set(media.VIDEO_PRESETS)


def test_recheck_hardware_forgets_and_retests(client, monkeypatch):
    calls = []
    monkeypatch.setattr(appmain.hardware, "forget", lambda: calls.append("forget"))
    monkeypatch.setattr(appmain.ffmpegtools, "refresh", lambda: calls.append("refresh") or ["libx264"])
    monkeypatch.setattr(appmain.hardware, "summary",
                        lambda: {"gpus": [], "nvidia_name": "", "gpu_ready": False})
    r = client.post("/api/hardware/recheck")
    assert r.status_code == 200
    assert calls == ["forget", "refresh"] and r.json()["encoders"] == ["libx264"]
    # Like every other change, it needs the page's key.
    bare = TestClient(appmain.app, base_url=BASE)
    assert bare.post("/api/hardware/recheck").status_code == 403


def test_the_nvidia_licence_link_can_be_opened():
    from app import assets
    assert appmain.url_allowed(assets.NVIDIA_LICENSE["url"])
    assert not appmain.url_allowed("https://docs.nvidia.com/somewhere/else")


# ---------------------------------------------------------- temp sweep

def test_an_old_job_folder_goes_in_one_sweep(tmp_path):
    """A crashed transcript's '<jid>_<hex>' folder: its old files are
    deleted, which touches the folder, and the folder still goes."""
    folder = tmp_path / "j12_0a1b2c3d"
    folder.mkdir()
    audio = folder / "audio.m4a"
    audio.write_bytes(b"x")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(audio, (long_ago, long_ago))
    os.utime(folder, (long_ago, long_ago))
    fresh = tmp_path / "j13_ffffffff"
    fresh.mkdir()
    (fresh / "audio.m4a").write_bytes(b"y")
    assert appmain.cleanup_temp(root=tmp_path) == 1
    assert not folder.exists()
    assert (fresh / "audio.m4a").exists()


# ------------------------------------------------------------- launcher

def _launch(monkeypatch, tmp_path, owns_lock: bool) -> list:
    from app import shell
    order = []
    monkeypatch.setattr(shell, "attach_console", lambda: True)
    monkeypatch.setattr(run, "_setup_log", lambda root, console: tmp_path / "app.log")
    monkeypatch.setattr(run, "_prepare_runtime", lambda: order.append("prepare"))
    monkeypatch.setattr(run.InstanceLock, "acquire", lambda self: owns_lock)
    monkeypatch.setattr(run.InstanceLock, "release", lambda self: None)
    monkeypatch.setattr(run, "find_running", lambda root, wait=0: {"port": 8950, "pid": 1})
    monkeypatch.setattr(run, "hold_instance", lambda info: True)
    monkeypatch.setattr(run, "open_ui", lambda url, browser, root: order.append("open"))
    monkeypatch.setattr(run, "_serve", lambda *a: order.append("serve") or 0)
    assert run.main(["--server"]) == 0
    return order


def test_a_second_launch_never_prepares_the_runtime(monkeypatch, tmp_path):
    """It only hands over; activate() must not wait on or touch the
    downloaded yt-dlp the running instance imports from."""
    assert _launch(monkeypatch, tmp_path, owns_lock=False) == []


def test_the_owner_prepares_the_runtime_before_serving(monkeypatch, tmp_path):
    assert _launch(monkeypatch, tmp_path, owns_lock=True) == ["prepare", "serve"]


def test_prepare_runtime_logs_which_yt_dlp_is_used(monkeypatch, capsys):
    from app import updater
    monkeypatch.setattr(updater, "activate",
                        lambda: {"active": True, "version": "2026.09.30", "reason": "newer"})
    run._prepare_runtime()
    assert "yt-dlp: downloaded 2026.09.30 (newer)" in capsys.readouterr().out


def test_probe_hardware_never_stops_startup(monkeypatch, capsys):
    from app import hardware
    monkeypatch.setattr(hardware, "prime", lambda: 1 / 0)
    run._probe_hardware()
    assert "hardware probe failed" in capsys.readouterr().out


# --------------------------------------------------------- live cookies

def _fake_ydl(monkeypatch, seen):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            seen.append(self.opts.get("cookiefile"))
            return self

        def __exit__(self, *exc):
            if self.opts.get("cookiefile"):
                Path(self.opts["cookiefile"]).write_text("clobbered", encoding="utf-8")

    monkeypatch.setattr(live, "YoutubeDL", FakeYDL)


def test_live_uses_the_engines_private_copy_as_is(monkeypatch, tmp_path):
    user = tmp_path / "cookies.txt"
    user.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(appmain.config, "get", lambda: {**appmain.config.DEFAULTS,
                                                         "cookies_file": str(user)})
    seen = []
    _fake_ydl(monkeypatch, seen)
    opts = live._opts()
    engine_copy = opts["cookiefile"]
    assert engine_copy != str(user) and live._engine_copy(engine_copy)
    with live._ydl(opts):
        pass
    assert seen == [engine_copy]                  # no second copy of the sign-in
    assert not Path(engine_copy).exists()         # handed back and deleted
    assert user.read_text(encoding="utf-8") == "# Netscape HTTP Cookie File\n"
