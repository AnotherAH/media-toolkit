"""Job registry (app/jobs.py): outcomes, cancel semantics, pools, history."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app import jobs


def _reset():
    deadline = time.time() + 5
    while jobs._running and time.time() < deadline:
        time.sleep(0.02)
    with jobs._lock:
        jobs._jobs.clear()
        jobs._cancels.clear()
        jobs._running.clear()
        jobs._started.clear()
        jobs._recipes.clear()
        jobs._trash.clear()


@pytest.fixture(autouse=True)
def clean_registry():
    _reset()
    yield
    _reset()


def wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def status(jid):
    job = jobs.get(jid)
    return job["status"] if job else None


def test_create_has_every_contract_field():
    job = jobs.create("download", "https://x.test/v", {"a": 1},
                      hints={"title": "Big Buck Bunny", "thumbnail": "t.jpg", "uploader": "Blender"})
    for key in ("id", "kind", "url", "title", "thumbnail", "uploader", "status", "stage",
                "stage_detail", "progress", "indeterminate", "bytes_done", "bytes_total",
                "speed_bps", "eta_s", "speed", "eta", "item", "steps", "live_phase",
                "rec_seconds", "rec_bytes", "rec_limit_seconds", "next_check_at", "give_up_at",
                "parts", "error", "message", "files", "result", "options", "created", "updated",
                "from_history"):
        assert key in job, key
    assert job["title"] == "Big Buck Bunny" and job["uploader"] == "Blender"
    assert job["status"] == "queued" and job["from_history"] is False


def test_progress_never_moves_backwards_within_a_stage():
    jid = jobs.create("download", "u")["id"]
    jobs.update(jid, status="running", stage="Downloading", progress=0.5)
    jobs.update(jid, progress=0.2)
    assert jobs.get(jid)["progress"] == 0.5
    jobs.update(jid, stage="Joining video and audio", progress=0.0)
    assert jobs.get(jid)["progress"] == 0.0


def test_add_file_infers_kind():
    jid = jobs.create("download", "u")["id"]
    for name in ("a.mp4", "b.mp3", "c.txt", "d.srt", "e.md", "f.info.json", "g.webp",
                 "h.description", "i.url", "j.part", "k.xyz"):
        jobs.add_file(jid, name, "media")
    kinds = [f["kind"] for f in jobs.get(jid)["files"]]
    assert kinds == ["video", "audio", "transcript", "subtitles", "notes", "json", "thumbnail",
                     "description", "link", "part", "other"]
    jobs.add_file(jid, "a.mp4")                       # no duplicates
    assert len(jobs.get(jid)["files"]) == 11
    assert jobs.get(jid)["files"][0]["ext"] == "mp4"


def test_steps():
    jid = jobs.create("transcript", "u")["id"]
    jobs.set_steps(jid, [("reading", "Reading the link"), ("captions", "Looking for captions")])
    jobs.step(jid, "reading", "done")
    jobs.step(jid, "captions", "active", note="English, from the uploader")
    jobs.step(jid, "model", "active", label="Downloading the speech model (one time only)")
    steps = jobs.get(jid)["steps"]
    assert [s["state"] for s in steps] == ["done", "active", "active"]
    assert steps[1]["note"] == "English, from the uploader"
    assert steps[2]["label"].startswith("Downloading the speech model")


def test_outcomes():
    def ok(jid, url, o):
        jobs.add_file(jid, "x.mp4")
        return {"meta": {}, "segments": [1, 2], "text": "hello"}

    def skip(jid, url, o):
        raise jobs.Skipped("Nothing new. All 3 videos were already downloaded.")

    def boom(jid, url, o):
        raise RuntimeError("ERROR: Unsupported URL: https://example.com/")

    ids = {}
    for name, fn in (("ok", ok), ("skip", skip), ("boom", boom)):
        ids[name] = jobs.create("download", "https://example.com/", {})["id"]
        jobs.submit(ids[name], fn, "https://example.com/", {})
    assert wait_for(lambda: all(status(j) in jobs.FINAL for j in ids.values()))

    done = jobs.get(ids["ok"])
    assert done["status"] == "done" and done["progress"] == 1.0
    assert done["result"]["text"] == "hello"                       # full by default
    slim = jobs.get(ids["ok"], slim=True)
    assert "segments" not in slim["result"] and "text" not in slim["result"]
    assert all("segments" not in (j["result"] or {}) for j in jobs.all_jobs())
    assert jobs.full_result(ids["ok"])["segments"] == [1, 2]

    skipped = jobs.get(ids["skip"])
    assert skipped["status"] == "skipped"
    assert skipped["stage"] == "Nothing new. All 3 videos were already downloaded."

    failed = jobs.get(ids["boom"])
    assert failed["status"] == "error"
    assert failed["error"]["code"] == "bad_link"
    assert failed["message"] == f"{failed['error']['title']} {failed['error']['body']}"


def test_cancelled_subclasses_yt_dlp_cancel():
    from yt_dlp.utils import DownloadCancelled
    assert issubclass(jobs.Cancelled, DownloadCancelled)


def test_cancel_running_download_goes_through_stopping():
    gate = threading.Event()

    def work(jid, url, o):
        gate.wait(5)
        jobs.raise_if_cancelled(jid)
        return {}

    jid = jobs.create("download", "u")["id"]
    jobs.submit(jid, work, "u", {})
    assert wait_for(lambda: status(jid) == "running")
    jobs.update(jid, speed="1 MB/s", speed_bps=1e6, eta="5s", eta_s=5)
    assert jobs.cancel(jid) is True
    job = jobs.get(jid)
    assert job["status"] == "stopping" and job["speed_bps"] is None and job["eta"] == ""
    assert jobs.active_count() == 1                   # still working until it returns
    assert jobs.cancel(jid) is False                  # already stopping
    gate.set()
    assert wait_for(lambda: status(jid) == "cancelled")
    assert jobs.active_count() == 0


def test_an_error_while_stopping_is_still_a_cancel():
    """ffmpeg killed mid-write by the cancel exits non-zero; the user asked to
    stop, so the card must read Cancelled, not a scary failure."""
    gate = threading.Event()

    def work(jid, url, o):
        gate.wait(5)
        raise RuntimeError("ERROR: ffmpeg exited with code 3436169992")

    jid = jobs.create("download", "u")["id"]
    jobs.submit(jid, work, "u", {})
    assert wait_for(lambda: status(jid) == "running")
    jobs.cancel(jid)
    gate.set()
    assert wait_for(lambda: status(jid) == "cancelled")
    assert jobs.get(jid)["error"] is None


def test_live_stop_is_saving_then_done_never_cancelled():
    gate = threading.Event()

    def record(jid, url, o):
        jobs.update(jid, live_phase="recording")
        while not jobs.cancelled(jid):
            time.sleep(0.01)
        gate.wait(5)                                  # "remuxing"
        jobs.add_file(jid, "rec.mp4")
        return {"detail": {"end_reason": "user"}}

    jid = jobs.create("live", "u")["id"]
    jobs.submit(jid, record, "u", {}, pool="live")
    assert wait_for(lambda: (jobs.get(jid) or {}).get("live_phase") == "recording")
    assert jobs.cancel(jid)
    job = jobs.get(jid)
    assert job["status"] == "stopping" and job["live_phase"] == "saving"
    assert jobs.clear_completed() == 0                # LIVE-3: cannot be cleared while saving
    assert jobs.active_count() == 1
    gate.set()
    assert wait_for(lambda: status(jid) == "done")
    assert jobs.get(jid)["files"][0]["name"] == "rec.mp4"


def test_live_stopped_while_waiting_without_files_is_cancelled():
    def wait(jid, url, o):
        jobs.update(jid, live_phase="waiting")
        while True:
            jobs.raise_if_cancelled(jid)
            time.sleep(0.01)

    jid = jobs.create("live", "u")["id"]
    jobs.submit(jid, wait, "u", {}, pool="live")
    assert wait_for(lambda: (jobs.get(jid) or {}).get("live_phase") == "waiting")
    jobs.cancel(jid)
    assert jobs.get(jid)["live_phase"] == "waiting"
    assert wait_for(lambda: status(jid) == "cancelled")


def test_live_jobs_never_take_a_worker_slot():
    """K15 / L-05: three recordings plus a download: the download still runs."""
    stop = threading.Event()

    def record(jid, url, o):
        while not stop.is_set() and not jobs.cancelled(jid):
            time.sleep(0.01)
        return {}

    lives = []
    for _ in range(4):
        jid = jobs.create("live", "u")["id"]
        jobs.submit(jid, record, "u", {}, pool="live")
        lives.append(jid)
    quick = jobs.create("download", "u")["id"]
    jobs.submit(quick, lambda jid, url, o: {"ok": True}, "u", {})
    try:
        assert wait_for(lambda: status(quick) == "done", timeout=3)
    finally:
        stop.set()
    assert wait_for(lambda: all(status(j) == "done" for j in lives))


def test_cancelled_is_true_for_unknown_ids():
    assert jobs.cancelled("j-does-not-exist") is True
    assert jobs.cancelled("") is False
    with pytest.raises(jobs.Cancelled):
        jobs.raise_if_cancelled("j-gone")


def test_cleared_queued_job_never_runs():
    """LIVE-3 (b): fill the pool, cancel and clear a queued job, free the pool."""
    release = threading.Event()
    ran = []

    def block(jid, url, o):
        release.wait(5)
        return {}

    blockers = []
    for _ in range(3):
        jid = jobs.create("download", "u")["id"]
        jobs.submit(jid, block, "u", {})
        blockers.append(jid)
    assert wait_for(lambda: all(status(j) == "running" for j in blockers))
    victim = jobs.create("download", "u")["id"]
    jobs.submit(victim, lambda jid, url, o: ran.append(jid) or {}, "u", {})
    assert jobs.cancel(victim)
    assert status(victim) == "cancelled"
    assert jobs.clear_completed() == 1
    assert jobs.get(victim) is None
    release.set()
    assert wait_for(lambda: all(status(j) == "done" for j in blockers))
    time.sleep(0.2)
    assert ran == [] and jobs.get(victim) is None


def test_retry_uncancels_a_queued_job():
    release = threading.Event()
    blockers = []
    for _ in range(3):
        jid = jobs.create("download", "u")["id"]
        jobs.submit(jid, lambda j, u, o: release.wait(5) and {}, "u", {})
        blockers.append(jid)
    assert wait_for(lambda: all(status(j) == "running" for j in blockers))
    victim = jobs.create("download", "u")["id"]
    jobs.submit(victim, lambda j, u, o: {"ran": True}, "u", {})
    jobs.cancel(victim)
    assert jobs.retry(victim)["status"] == "queued"
    release.set()
    assert wait_for(lambda: status(victim) == "done")
    assert jobs.get(victim)["result"] == {"ran": True}


def test_clear_restore_remove_and_retry():
    calls = []

    def work(jid, url, o):
        calls.append(dict(o))
        if o.get("fail"):
            raise RuntimeError("HTTP Error 429")
        return {"n": len(calls)}

    good = jobs.create("download", "u1", {})["id"]
    bad = jobs.create("download", "u2", {"fail": True})["id"]
    jobs.submit(good, work, "u1", {})
    jobs.submit(bad, work, "u2", {"fail": True})
    assert wait_for(lambda: status(good) == "done" and status(bad) == "error")

    assert jobs.clear_completed() == 1                # failures stay
    assert jobs.get(good) is None and status(bad) == "error"
    assert jobs.restore() == 1 and status(good) == "done"
    assert jobs.restore() == 0

    fresh = jobs.retry(bad, {"fail": False})
    assert fresh["id"] == bad and fresh["status"] in ("queued", "running", "done")
    assert wait_for(lambda: status(bad) == "done")
    assert calls[-1]["fail"] is False and jobs.get(bad)["error"] is None

    assert jobs.remove(good) is True and jobs.get(good) is None
    assert jobs.remove("nope") is False


def test_remove_refuses_active_jobs():
    gate = threading.Event()
    jid = jobs.create("download", "u")["id"]
    jobs.submit(jid, lambda j, u, o: gate.wait(5) and {}, "u", {})
    assert wait_for(lambda: status(jid) == "running")
    assert jobs.remove(jid) is False
    assert jobs.retry(jid) is None
    gate.set()
    assert wait_for(lambda: status(jid) == "done")


def test_error_params_fill_the_card():
    from app.errors import AppError

    def not_live(jid, url, o):
        raise AppError("live_not_live", "not live", title="ISS", thumbnail="t.jpg", uploader="NASA")

    jid = jobs.create("live", "https://www.youtube.com/@NASA/live")["id"]
    jobs.submit(jid, not_live, "u", {}, pool="live")
    assert wait_for(lambda: status(jid) == "error")
    job = jobs.get(jid)
    assert job["title"] == "ISS" and job["thumbnail"] == "t.jpg" and job["uploader"] == "NASA"
    assert job["error"]["code"] == "live_not_live" and job["error"]["actions"] == ["retry_wait"]


def test_upload_copy_deleted_when_done_kept_on_error(tmp_path):
    folder_ok = jobs.UPLOAD_ROOT / "test-ok"
    folder_bad = jobs.UPLOAD_ROOT / "test-bad"
    for f in (folder_ok, folder_bad):
        f.mkdir(parents=True, exist_ok=True)
        (f / "clip.wav").write_bytes(b"x")
    ok = jobs.create("transcript", "", {"local_path": str(folder_ok / "clip.wav")})["id"]
    bad = jobs.create("transcript", "", {"local_path": str(folder_bad / "clip.wav")})["id"]
    jobs.submit(ok, lambda j, u, o: {}, "", {"local_path": str(folder_ok / "clip.wav")})

    def fail(j, u, o):
        raise RuntimeError("Invalid data found when processing input")

    jobs.submit(bad, fail, "", {"local_path": str(folder_bad / "clip.wav")})
    assert wait_for(lambda: status(ok) == "done" and status(bad) == "error")
    assert wait_for(lambda: not folder_ok.exists())
    assert (folder_bad / "clip.wav").exists()            # kept for Retry
    jobs.remove(bad)
    assert not folder_bad.exists()


def test_history_round_trip(data_root):
    def ok(jid, url, o):
        return {"meta": {"title": "T"}, "segments": [{"start": 0, "end": 1, "text": "x"}],
                "text": "x", "output_dir": "C:/t", "stem": "T"}

    jid = jobs.create("transcript", "https://youtu.be/x")["id"]
    jobs.submit(jid, ok, "https://youtu.be/x", {})
    assert wait_for(lambda: status(jid) == "done")
    assert wait_for(lambda: jobs.history_path().exists())
    saved = json.loads(jobs.history_path().read_text("utf-8"))["jobs"]
    rec = next(r for r in saved if r["id"] == jid)
    assert "segments" not in rec["result"] and "text" not in rec["result"]

    _reset()
    assert jobs.load_history() >= 1
    back = jobs.get(jid)
    assert back["from_history"] is True and back["status"] == "done"
    new = jobs.create("download", "u")["id"]
    assert int(new[1:]) > int(jid[1:])                     # ids never collide


def test_history_is_capped(data_root):
    with jobs._lock:
        for i in range(jobs.HISTORY_LIMIT + 20):
            job = jobs._new_job(f"j{100000 + i}", "download", "u", {}, "", {})
            job["status"] = "done"
            job["updated"] = i
            jobs._jobs[job["id"]] = job
    jobs.save_history()
    saved = json.loads(jobs.history_path().read_text("utf-8"))["jobs"]
    assert len(saved) == jobs.HISTORY_LIMIT
    assert saved[0]["id"] == f"j{100000 + jobs.HISTORY_LIMIT + 19}"     # newest kept


def test_history_ignores_garbage(data_root):
    jobs.history_path().write_text("{not json", encoding="utf-8")
    assert jobs.load_history() == 0
    jobs.history_path().write_text(json.dumps({"jobs": [{"id": "j1", "status": "running"}, 5]}),
                                   encoding="utf-8")
    assert jobs.load_history() == 0


def test_retry_uses_registered_runner_for_history_jobs():
    seen = []
    jobs.register("download", lambda jid, url, o: seen.append((url, o)) or {"ok": 1})
    with jobs._lock:
        job = jobs._new_job("j900001", "download", "https://example.com/v", {"q": 1}, "T", {})
        job.update(status="error", from_history=True)
        jobs._jobs[job["id"]] = job
        jobs._cancels[job["id"]] = threading.Event()
    assert jobs.retry("j900001")["from_history"] is False
    assert wait_for(lambda: status("j900001") == "done")
    assert seen == [("https://example.com/v", {"q": 1})]
