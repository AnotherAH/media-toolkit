"""The download run (app/media.py _Run, run_download), the app's own
postprocessors (app/recode.py) and encoder probing (app/ffmpegtools.py).

Offline: yt-dlp is replaced by a small fake that calls the same hooks the
real one does, so outcomes (limit, up to date, skipped, per-item failures,
cancel) are checked without a network. One test marked "network" downloads
a real 3-second clip.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from yt_dlp.utils import ExistingVideoReached, MaxDownloadsReached

from app import config, ffmpegtools, jobs, media, recode


@pytest.fixture(autouse=True)
def setup(tmp_path):
    keep = config.get()
    config.save({**config.DEFAULTS, "download_dir": str(tmp_path / "dl"),
                 "transcript_dir": str(tmp_path / "tr"), "use_temp_dir": False,
                 "setup_complete": True})
    yield
    config.save(keep)


def new_job(url="https://example.com/v"):
    return jobs.create("download", url, {})["id"]


class FakeYDL:
    """Enough of YoutubeDL for run_download: a script decides what
    extract_info returns and what processing does (calling the run's hooks
    through the options, as yt-dlp would)."""

    def __init__(self, opts, ie, process):
        self.params = opts
        self._ie = ie
        self._process = process

    def in_download_archive(self, info):
        return bool(info.get("archived"))

    def extract_info(self, url, download=False, process=True, ie_key=None):
        return self._ie(self) if callable(self._ie) else self._ie

    def process_ie_result(self, ie, download=True):
        return self._process(self, ie)


def use_fake(monkeypatch, ie, process):
    import contextlib

    @contextlib.contextmanager
    def fake_session(opts):
        yield FakeYDL(opts, ie, process)

    monkeypatch.setattr(media, "session", fake_session)
    monkeypatch.setattr(media, "_make_pps", lambda ydl, run: None)


def moved(ydl, path: Path, vid="a"):
    """What yt-dlp's MoveFiles hook reports once an item is in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    for hook in ydl.params["postprocessor_hooks"]:
        hook({"status": "finished", "postprocessor": "MoveFiles",
              "info_dict": {"id": vid, "filepath": str(path), "__finaldir": str(path.parent),
                            "__files_to_move": {}}})


# ---------------------------------------------------------------- outcomes

def test_single_video_saved(monkeypatch, tmp_path):
    out = tmp_path / "dl" / "Clip [a].mp4"

    def process(ydl, ie):
        moved(ydl, out)
        return {"id": "a", "title": "Clip", "requested_downloads": [{"filepath": str(out)}]}

    use_fake(monkeypatch, {"id": "a", "title": "Clip", "thumbnail": "t.jpg"}, process)
    jid = new_job()
    result = media.run_download(jid, "https://example.com/v", {})
    assert result["saved"] == 1 and result["count"] == 1 and result["stop_reason"] is None
    assert result["items_failed"] == [] and result["playlist"] is False
    job = jobs.get(jid)
    assert [f["name"] for f in job["files"]] == ["Clip [a].mp4"]
    assert job["title"] == "Clip" and job["thumbnail"] == "t.jpg"


def test_request_output_dir_is_ignored(monkeypatch, tmp_path):
    seen = {}

    def process(ydl, ie):
        seen["home"] = ydl.params["paths"]["home"]
        return {"id": "a"}

    use_fake(monkeypatch, {"id": "a", "title": "x"}, process)
    with pytest.raises(RuntimeError, match="Nothing was downloaded"):
        media.run_download(new_job(), "u", {"output_dir": str(tmp_path / "elsewhere")})
    assert seen["home"] == str(tmp_path / "dl")
    assert not (tmp_path / "elsewhere").exists()


def test_limit_reached_is_a_success(monkeypatch, tmp_path):
    def process(ydl, ie):
        moved(ydl, tmp_path / "dl" / "PL" / "001 - one [a].mp4", "a")
        raise MaxDownloadsReached()

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    jid = new_job()
    result = media.run_download(jid, "u", {"playlist_mode": "all", "max_downloads": 1})
    assert result["stop_reason"] == "limit" and result["saved"] == 1
    assert len(jobs.get(jid)["files"]) == 1


def test_first_n_of_a_longer_playlist_stops_at_the_limit(monkeypatch, tmp_path):
    def process(ydl, ie):
        moved(ydl, tmp_path / "dl" / "PL" / "001 - one [a].mp4", "a")
        moved(ydl, tmp_path / "dl" / "PL" / "002 - two [b].mp4", "b")
        return {"_type": "playlist", "id": "PL"}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL", "playlist_count": 8},
             process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "first", "playlist_first": 2})
    assert result["stop_reason"] == "limit" and result["saved"] == 2


def test_first_n_covering_the_whole_playlist_is_not_a_limit(monkeypatch, tmp_path):
    def process(ydl, ie):
        moved(ydl, tmp_path / "dl" / "PL" / "001 - one [a].mp4", "a")
        return {"_type": "playlist", "id": "PL"}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL", "playlist_count": 1},
             process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "first", "playlist_first": 10})
    assert result["stop_reason"] is None and result["saved"] == 1


def test_up_to_date_with_new_items(monkeypatch, tmp_path):
    def process(ydl, ie):
        moved(ydl, tmp_path / "dl" / "PL" / "001 - new [n].mp4", "n")
        raise ExistingVideoReached()

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "all", "archive": True,
                                                 "stop_at_known": True})
    assert result["stop_reason"] == "up_to_date" and result["saved"] == 1


def test_up_to_date_with_nothing_new_is_skipped(monkeypatch):
    def process(ydl, ie):
        raise ExistingVideoReached()

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    with pytest.raises(jobs.Skipped, match="Nothing new"):
        media.run_download(new_job(), "u", {"playlist_mode": "all", "archive": True,
                                            "stop_at_known": True})


def test_everything_archived(monkeypatch):
    def process(ydl, ie):
        for i in range(3):
            ydl.in_download_archive({"id": f"v{i}", "archived": True, "n_entries": 3})
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    with pytest.raises(jobs.Skipped) as exc:
        media.run_download(new_job(), "u", {"playlist_mode": "all", "archive": True})
    assert exc.value.reason == "Nothing new. All 3 videos were already downloaded."


def test_single_archived_video(monkeypatch):
    def ie(ydl):
        ydl.in_download_archive({"id": "a", "archived": True})
        return None

    use_fake(monkeypatch, ie, lambda ydl, ie: None)
    with pytest.raises(jobs.Skipped) as exc:
        media.run_download(new_job(), "u", {"archive": True})
    assert exc.value.reason == "Nothing new. The video was already downloaded."


def test_filtered_single_video_is_skipped_with_reason(monkeypatch):
    def process(ydl, ie):
        info = {"id": "a", "title": "Long film", "duration": 600}
        assert ydl.params["match_filter"](info, incomplete=False)
        return info

    use_fake(monkeypatch, {"id": "a", "title": "Long film"}, process)
    jid = new_job()
    with pytest.raises(jobs.Skipped) as exc:
        media.run_download(jid, "u", {"max_duration": 1})
    assert exc.value.reason == "Skipped: longer than your 1-minute limit"
    assert jobs.get(jid)["title"] == "Long film"       # never a bare URL


def test_live_link_is_skipped(monkeypatch):
    def process(ydl, ie):
        ydl.params["match_filter"]({"id": "a", "is_live": True}, incomplete={"format"})
        return {"id": "a"}

    use_fake(monkeypatch, {"id": "a", "title": "Live"}, process)
    with pytest.raises(jobs.Skipped, match="Skipped: a live stream"):
        media.run_download(new_job(), "u", {})


def test_playlist_counts_saved_failed_and_skipped(monkeypatch, tmp_path):
    def process(ydl, ie):
        f = ydl.params["match_filter"]
        f({"id": "a", "title": "One", "url": "https://y/a", "playlist_autonumber": 1,
           "n_entries": 3}, incomplete=True)
        moved(ydl, tmp_path / "dl" / "PL" / "001 - One [a].mp4", "a")
        f({"id": "b", "title": "Private one", "url": "https://y/b", "playlist_autonumber": 2,
           "n_entries": 3}, incomplete=True)
        ydl.params["logger"].error("ERROR: [youtube] b: Private video. Sign in if you've "
                                   "been granted access to this video")
        f({"id": "c", "title": "Long", "url": "https://y/c", "duration": 9999,
           "playlist_autonumber": 3, "n_entries": 3}, incomplete=True)
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "all", "max_duration": 60})
    assert result["saved"] == 1 and result["failed"] == 1 and result["skipped"] == 1
    failed = result["items_failed"][0]
    assert failed["title"] == "Private one" and failed["url"] == "https://y/b"
    assert failed["error"] == "You need to be signed in to get this"
    assert failed["code"] == "signin"


def test_playlist_where_everything_failed_is_an_error(monkeypatch):
    def process(ydl, ie):
        ydl.params["match_filter"]({"id": "b", "title": "B", "url": "https://y/b",
                                    "playlist_autonumber": 1, "n_entries": 1}, incomplete=True)
        ydl.params["logger"].error("ERROR: [youtube] b: Video unavailable")
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    with pytest.raises(RuntimeError, match="unavailable"):
        media.run_download(new_job(), "u", {"playlist_mode": "all"})


def test_playlist_options_applied_only_to_playlists(monkeypatch):
    seen = {}

    def process(ydl, ie):
        seen["default"] = ydl.params["outtmpl"]["default"]
        seen["ignoreerrors"] = ydl.params["ignoreerrors"]
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    with pytest.raises(jobs.Skipped):
        media.run_download(new_job(), "u", {"playlist_mode": "all"})
    assert "%(playlist_index)03d" in seen["default"]
    assert seen["ignoreerrors"] == "only_download"

    def single(ydl, ie):
        seen["default"] = ydl.params["outtmpl"]["default"]
        seen["ignoreerrors"] = ydl.params["ignoreerrors"]
        return {"id": "a"}

    use_fake(monkeypatch, {"id": "a", "title": "x"}, single)
    with pytest.raises(RuntimeError):
        media.run_download(new_job(), "u", {"playlist_mode": "all"})
    assert "playlist_index" not in seen["default"] and seen["ignoreerrors"] is False


def test_empty_playlist(monkeypatch):
    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"},
             lambda ydl, ie: {"_type": "playlist", "entries": []})
    with pytest.raises(jobs.Skipped, match="no videos"):
        media.run_download(new_job(), "u", {"playlist_mode": "all"})


def test_missing_chapter_is_explained(monkeypatch):
    def process(ydl, ie):
        info = {"id": "a", "duration": 60, "chapters": [{"title": "Intro", "start_time": 0,
                                                         "end_time": 5}]}

        class Y:
            def to_screen(self, *_):
                pass
        list(ydl.params["download_ranges"](info, Y()))
        return info

    use_fake(monkeypatch, {"id": "a", "title": "x"}, process)
    with pytest.raises(jobs.Skipped, match="no chapter is named “credits”"):
        media.run_download(new_job(), "u", {"section": "credits"})


def test_errors_are_raised_not_swallowed(monkeypatch):
    from yt_dlp.utils import DownloadError

    def process(ydl, ie):
        raise DownloadError("ERROR: [youtube] a: Video unavailable")

    use_fake(monkeypatch, {"id": "a", "title": "x"}, process)
    with pytest.raises(DownloadError):
        media.run_download(new_job(), "u", {})


def test_concat_failure_keeps_the_items(monkeypatch, tmp_path):
    from yt_dlp.utils import PostProcessingError

    def process(ydl, ie):
        moved(ydl, tmp_path / "dl" / "PL" / "001 - a [a].mp4", "a")
        raise PostProcessingError("Aborting concatenation because some downloads failed")

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "all", "concat_playlist": True})
    assert result["saved"] == 1
    assert any("Couldn't join" in n for n in result["notes"])


def test_cancel_cleans_this_jobs_partials_only(monkeypatch, tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir(parents=True)
    old_thumb = dl / "Clip [a].webp"
    old_thumb.write_bytes(b"old")
    os.utime(old_thumb, (time.time() - 3600, time.time() - 3600))
    kept = dl / "Clip [a].mp4"                       # an earlier, finished download
    kept.write_bytes(b"old media")
    os.utime(kept, (time.time() - 3600, time.time() - 3600))
    jid = new_job()

    def process(ydl, ie):
        part = dl / "Clip [a].f137.mp4.part"
        part.write_bytes(b"x")
        (dl / "Clip [a].jpg").write_bytes(b"thumb")
        (dl / "Clip [a].en.srt").write_bytes(b"subs")
        ydl.params["progress_hooks"][0]({"status": "error", "tmpfilename": str(part),
                                         "filename": str(dl / "Clip [a].f137.mp4"),
                                         "info_dict": {}})
        jobs.cancel(jid)
        raise jobs.Cancelled()

    use_fake(monkeypatch, {"id": "a", "title": "Clip"}, process)
    with pytest.raises(jobs.Cancelled):
        media.run_download(jid, "u", {})
    assert sorted(p.name for p in dl.iterdir()) == ["Clip [a].mp4", "Clip [a].webp"]


# ------------------------------------------------------- premiere wait

class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def fake_poll(monkeypatch, answers, on_call=None):
    import contextlib
    calls = []

    @contextlib.contextmanager
    def fake_session(opts):
        class Y:
            def extract_info(self, url, download=False, process=True):
                calls.append(url)
                if on_call:
                    on_call(len(calls))
                answer = answers[min(len(calls), len(answers)) - 1]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        yield Y()

    monkeypatch.setattr(media, "session", fake_session)
    clock = FakeClock()
    monkeypatch.setattr(media, "time", clock)
    return calls, clock


def test_wait_for_release_polls_until_published(monkeypatch):
    from yt_dlp.utils import DownloadError
    calls, clock = fake_poll(monkeypatch, [
        {"live_status": "is_upcoming", "release_timestamp": 1_000_000 + 600, "title": "Premiere"},
        DownloadError("ERROR: [youtube] x: Premieres in 5 minutes"),
        {"live_status": "not_live"},
    ])
    jid = new_job()
    media._wait_for_release(jid, "u", {})
    assert len(calls) == 3
    job = jobs.get(jid)
    assert job["title"] == "Premiere" and job["next_check_at"] is None


def test_wait_for_release_stops_on_cancel(monkeypatch):
    jid = new_job()
    calls, clock = fake_poll(monkeypatch, [{"live_status": "is_upcoming"}],
                             on_call=lambda n: n == 2 and jobs.cancel(jid))
    jobs.update(jid, status="running")
    with pytest.raises(jobs.Cancelled):
        media._wait_for_release(jid, "u", {})
    assert len(calls) == 2


def test_wait_for_release_gives_up(monkeypatch):
    from app.errors import AppError
    fake_poll(monkeypatch, [{"live_status": "is_upcoming", "title": "Show"}])
    with pytest.raises(AppError) as exc:
        media._wait_for_release(new_job(), "u", {"wait_minutes": 5})
    assert exc.value.code == "live_not_live"


def test_wait_for_release_lets_real_errors_through(monkeypatch):
    from yt_dlp.utils import DownloadError
    calls, _ = fake_poll(monkeypatch, [DownloadError("ERROR: Video unavailable")])
    media._wait_for_release(new_job(), "u", {})          # returns; the download reports it
    assert len(calls) == 1


# ------------------------------------------------------------- progress

def make_run(opts=None, playlist=False):
    jid = new_job()
    o = media.build_download_opts(dict(opts or {}), "C:/dl", None)
    run = media._Run(jid, "u", {}, o, "C:/dl")
    run.is_playlist = playlist
    return jid, run


def test_progress_combines_streams_and_never_goes_back(monkeypatch):
    monkeypatch.setattr(media, "_UPDATE_EVERY", 0)
    jid, run = make_run()
    jobs.update(jid, status="running")
    run.on_video({"requested_formats": [
        {"format_id": "137", "vcodec": "avc1", "filesize": 800},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 200}]})
    seen = []
    for fid, done, total in (("137", 400, 800), ("137", 800, 800), ("140", 0, 200),
                             ("140", 100, 200), ("140", 200, 200)):
        run.progress({"status": "downloading", "downloaded_bytes": done, "total_bytes": total,
                      "speed": 100.0, "info_dict": {"format_id": fid}})
        job = jobs.get(jid)
        seen.append(job["progress"])
        assert job["bytes_total"] == 1000
    assert seen == sorted(seen)
    assert seen[0] == pytest.approx(0.97 * 0.4)
    assert seen[-1] == pytest.approx(0.97)
    job = jobs.get(jid)
    assert job["bytes_done"] == 1000 and job["speed_bps"] == 100.0 and job["eta_s"] == 0
    assert job["speed"] and job["stage"] == "Downloading"


def test_progress_with_unknown_sizes_weights_picture_then_sound(monkeypatch):
    monkeypatch.setattr(media, "_UPDATE_EVERY", 0)
    jid, run = make_run()
    jobs.update(jid, status="running")
    run.on_video({"requested_formats": [{"format_id": "v", "vcodec": "vp9"},
                                        {"format_id": "a", "vcodec": "none"}]})
    run.progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100,
                  "info_dict": {"format_id": "v"}})
    assert jobs.get(jid)["progress"] == pytest.approx(0.97 * 0.85 * 0.5)
    run.progress({"status": "finished", "total_bytes": 100, "info_dict": {"format_id": "v"}})
    run.progress({"status": "downloading", "downloaded_bytes": 10,
                  "info_dict": {"format_id": "a"}})       # sound of unknown size
    job = jobs.get(jid)
    assert job["progress"] == pytest.approx(0.97 * 0.85)
    assert job["bytes_total"] is None and job["bytes_done"] == 110


def test_playlist_item_and_overall_progress(monkeypatch):
    monkeypatch.setattr(media, "_UPDATE_EVERY", 0)
    jid, run = make_run(playlist=True)
    run.count = 4
    jobs.update(jid, status="running")
    run.on_video({"format_id": "18", "filesize": 100, "vcodec": "avc1", "title": "Third",
                  "playlist_autonumber": 3, "playlist_index": 7})
    job = jobs.get(jid)
    assert job["item"] == {"index": 3, "count": 4, "title": "Third"}
    assert job["progress"] == pytest.approx(0.5)
    run.progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100,
                  "info_dict": {"format_id": "18"}})
    assert jobs.get(jid)["progress"] == pytest.approx((2 + 0.97 * 0.5) / 4)


def test_postprocessing_is_indeterminate_and_plain(monkeypatch):
    jid, run = make_run({"mode": "audio", "audio_codec": "mp3"})
    jobs.update(jid, status="running")
    run.pp_hook({"status": "started", "postprocessor": "ExtractAudio", "info_dict": {}})
    job = jobs.get(jid)
    assert job["stage"] == "Converting to MP3…" and job["indeterminate"] is True
    assert job["progress"] == pytest.approx(0.97)
    run.pp_hook({"status": "started", "postprocessor": "RequestName", "info_dict": {}})
    assert jobs.get(jid)["stage"] == "Converting to MP3…"


# ------------------------------------------------------------ extra files

def test_collect_attaches_sidecars_and_renames_description(tmp_path):
    jid, run = make_run()
    d = tmp_path
    main = d / "Film [a].mp4"
    for name in ("Film [a].mp4", "Film [a].description", "Film [a].info.json", "Film [a].url",
                 "Film [a].en.srt", "Film [a].jpg", "Film [a] (720p).mp4",
                 "Film [a].f137.mp4.part", "Other [b].jpg"):
        (d / name).write_bytes(b"x")
    run._collect({"id": "a", "filepath": str(main), "__finaldir": str(d), "__files_to_move": {}})
    run.attach()
    files = {f["name"]: (f["kind"], f["label"]) for f in jobs.get(jid)["files"]}
    assert files == {
        "Film [a].mp4": ("video", ""),
        "Film [a].description.txt": ("description", "Description"),
        "Film [a].info.json": ("json", "Technical details"),
        "Film [a].url": ("link", "Shortcut to the page"),
        "Film [a].en.srt": ("subtitles", "Subtitles (en)"),
        "Film [a].jpg": ("thumbnail", "Thumbnail"),
    }
    assert list(jobs.get(jid)["files"])[0]["name"] == "Film [a].mp4"


def test_comments_file_is_readable(tmp_path):
    path = tmp_path / "c.txt"
    comments = [{"id": "1", "author": "Ana", "like_count": 1200, "text": "Great\nfilm",
                 "parent": "root"},
                {"id": "2", "author": "Bo", "text": "Agreed", "parent": "1"},
                {"id": "3", "author": "Cy", "text": "Third", "parent": "root"}]
    assert media._write_comments(str(path), comments, 1)
    text = path.read_text(encoding="utf-8")
    assert "Ana · 1,200 likes" in text and "Great\nfilm" in text
    assert "    Bo" in text and "Cy" not in text


def test_split_chapter_files_are_attached(tmp_path):
    jid, run = make_run()
    ch = tmp_path / "Film - 001 Intro [a].mp4"
    ch.write_bytes(b"x")
    run.pp_hook({"status": "finished", "postprocessor": "SplitChapters",
                 "info_dict": {"chapters": [{"title": "Intro", "filepath": str(ch)}]}})
    run.attach()
    assert jobs.get(jid)["files"][0]["label"] == "Chapter 1: Intro"


# ------------------------------------------------------- probe summaries

def test_summarize_video_fields():
    info = {"id": "a", "title": "T", "duration": 100, "webpage_url": "https://youtu.be/a",
            "live_status": "is_upcoming", "release_timestamp": 1900000000,
            "subtitles": {"en": [{}], "live_chat": [{}]},
            "formats": [
                {"format_id": "137", "height": 1080, "fps": 59.94, "vcodec": "avc1",
                 "acodec": "none", "filesize": 1000, "protocol": "https"},
                {"format_id": "399", "height": 1080, "fps": 60, "vcodec": "av01",
                 "acodec": "none", "filesize": 500, "protocol": "https"},
                {"format_id": "312", "height": 1080, "vcodec": "avc1", "acodec": "none",
                 "tbr": 9000, "protocol": "m3u8_native"},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 100,
                 "protocol": "https", "language_preference": -1},
                {"format_id": "233-0", "vcodec": "none", "acodec": "mp4a", "filesize": 999,
                 "protocol": "https", "language_preference": -10},
            ]}
    s = media.summarize(info)
    assert s["site"] == "YouTube" and s["max_fps"] == 60
    assert s["size_by_height"] == {2160: 1100, 1440: 1100, 1080: 1100}
    assert s["audio_size"] == 100 and s["size_best"] == s["size_smallest"] == 1100
    assert s["live_status"] == "is_upcoming" and s["release_timestamp"] == 1900000000
    assert s["subtitles"] == ["en"]


def test_sizes_follow_the_quality_caps():
    formats = [
        {"format_id": "w1080", "width": 1920, "height": 800, "vcodec": "avc1", "acodec": "none",
         "filesize": 900, "protocol": "https"},
        {"format_id": "w720", "width": 1280, "height": 534, "vcodec": "avc1", "acodec": "none",
         "filesize": 400, "protocol": "https"},
        {"format_id": "v1080", "width": 1080, "height": 1920, "vcodec": "vp9", "acodec": "none",
         "filesize": 700, "protocol": "https"},
        {"format_id": "a", "vcodec": "none", "acodec": "opus", "filesize": 50, "protocol": "https"},
    ]
    wide = [f for f in formats if f["format_id"] != "v1080"]
    by_cap, audio, best, smallest = media._sizes(wide, 100)
    assert by_cap[1080] == 950          # a 1920x800 film is the 1080p choice
    assert by_cap[720] == 450 and 480 not in by_cap
    assert best == 950 and smallest == 450 and audio == 50
    by_cap, *_ = media._sizes(formats, 100)
    assert by_cap[1080] == 750          # a 1080x1920 short is 1080p, not "1920p"
    assert 2160 in by_cap and by_cap[2160] == 750


def test_summarize_playlist_and_channel():
    info = {"_type": "playlist", "id": "UC1", "title": "NASA - Videos",
            "webpage_url": "https://www.youtube.com/@NASA/videos",
            "entries": [{"title": f"v{i}", "url": f"https://y/{i}"} for i in range(200)]}
    s = media.summarize(info)
    assert s["kind"] == "playlist" and s["is_channel"] is True
    assert s["count"] == 200 and s["count_more"] is True
    s = media.summarize({**info, "playlist_count": 74,
                         "webpage_url": "https://www.youtube.com/playlist?list=PL1"})
    assert s["count"] == 74 and not s["count_more"] and s["is_channel"] is False


def test_playlist_meta_never_touches_lazy_entries():
    def boom():
        raise AssertionError("entries were consumed")
        yield
    meta = media._playlist_meta({"_type": "playlist", "id": "PL", "title": "T",
                                 "entries": boom(), "webpage_url": "https://youtube.com/playlist?list=PL"})
    assert meta["title"] == "T" and meta["site"] == "YouTube"


def test_format_rows_labels_and_duplicates():
    info = {"duration": 100, "formats": [
        {"format_id": "sb0", "format_note": "storyboard", "ext": "mhtml", "vcodec": "none",
         "acodec": "none"},
        {"format_id": "299", "height": 1080, "fps": 60, "vcodec": "avc1.64", "acodec": "none",
         "filesize": 245 * 2 ** 20, "protocol": "https", "tbr": 3000, "ext": "mp4"},
        {"format_id": "312", "height": 1080, "fps": 60, "vcodec": "avc1.64", "acodec": "none",
         "protocol": "m3u8_native", "tbr": 7000, "ext": "mp4"},
        {"format_id": "251-drc", "vcodec": "none", "acodec": "opus", "abr": 130, "filesize": 12 * 2 ** 20,
         "protocol": "https", "tbr": 131, "ext": "webm"},
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "abr": 129, "filesize": 12 * 2 ** 20,
         "protocol": "https", "tbr": 129, "ext": "webm"},
        {"format_id": "233", "vcodec": "none", "acodec": None, "protocol": "m3u8_native", "ext": "mp4"},
        {"format_id": "18", "height": 360, "vcodec": "avc1.42", "acodec": "mp4a.40.2",
         "filesize_approx": 28 * 2 ** 20, "protocol": "https", "ext": "mp4"},
    ]}
    rows = {r["format_id"]: r for r in media._format_rows(info)}
    assert set(rows) == {"299", "251", "18"}
    assert rows["299"]["label"] == "1080p · 60 fps · H.264 · 245 MB · video only, best audio added"
    assert rows["251"]["label"] == "Audio · Opus · 129 kbps · 12 MB"
    assert rows["18"]["label"] == "360p · H.264 + AAC · 28 MB"
    assert rows["299"]["kind"] == "video" and rows["18"]["kind"] == "av"


def test_list_formats_refuses_playlists(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def fake_session(opts):
        assert opts["extract_flat"] == "in_playlist" and opts["noplaylist"] is True

        class Y:
            def extract_info(self, url, download=False):
                return {"_type": "playlist", "entries": []}
        yield Y()

    monkeypatch.setattr(media, "session", fake_session)
    from app.errors import AppError
    with pytest.raises(AppError) as exc:
        media.list_formats("https://www.youtube.com/playlist?list=PL")
    assert exc.value.code == "bad_link"


def test_probe_limits_playlists_and_marks_videos_in_lists(monkeypatch):
    import contextlib
    seen = {}

    @contextlib.contextmanager
    def fake_session(opts):
        seen.update(opts)

        class Y:
            def extract_info(self, url, download=False):
                return {"id": "a", "title": "T", "webpage_url": url}
        yield Y()

    monkeypatch.setattr(media, "session", fake_session)
    out = media.probe("https://www.youtube.com/watch?v=a&list=PL1")
    assert seen["playlistend"] == 200 and seen["noplaylist"] is True
    assert out["in_playlist"] is True and out["site"] == "YouTube"
    out = media.probe("https://www.youtube.com/playlist?list=PL1")
    assert seen["noplaylist"] is False and out["in_playlist"] is False


# -------------------------------------------------------- naming (recode)

class FakeDownloader:
    def __init__(self, folder, final_ext=None):
        self.folder = folder
        self.params = {"final_ext": final_ext}

    def prepare_filename(self, info, *a, **k):
        return str(self.folder / f"{info['title']} [{info['id']}]{info.get('mt_suffix', '')}.{info['ext']}")

    def to_screen(self, *a, **k):
        pass


def name_pp(folder, probe, monkeypatch, **kw):
    monkeypatch.setattr(ffmpegtools, "probe", probe)
    pp = recode.RequestNamePP(None, **kw)
    pp._downloader = FakeDownloader(folder, kw.pop("final_ext", None))
    pp._progress_hooks = []
    pp.to_screen = lambda *a, **k: None
    return pp


def test_clip_label_and_clock():
    assert recode.clock(10) == "0.10" and recode.clock(3723) == "1.02.03"
    assert recode.section_label({"section_start": 10, "section_end": 14}) == " (0.10-0.14)"
    assert recode.section_label({"section_start": 600, "section_end": None}) == " (10.00-end)"
    assert recode.section_label({"section_title": "Intro", "section_start": 0}) == " (Intro)"
    assert recode.section_label({}) == ""


def test_new_file_when_nothing_exists(tmp_path, monkeypatch):
    pp = name_pp(tmp_path, lambda p: {}, monkeypatch)
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 1080, "vcodec": "avc1",
            "duration": 100}
    pp._name(info)
    assert info["mt_suffix"] == "" and "__mt_reused" not in info


def test_same_file_is_reused(tmp_path, monkeypatch):
    (tmp_path / "T [a].mp4").write_bytes(b"x")
    pp = name_pp(tmp_path, lambda p: {"height": 1080, "vcodec": "h264", "duration": 100.5},
                 monkeypatch)
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 1080, "vcodec": "avc1.64",
            "duration": 100}
    pp._name(info)
    assert info["__mt_reused"] is True and info["mt_suffix"] == ""


def test_other_quality_gets_its_own_name(tmp_path, monkeypatch):
    (tmp_path / "T [a].mp4").write_bytes(b"x")
    pp = name_pp(tmp_path, lambda p: {"height": 480, "vcodec": "h264", "duration": 100},
                 monkeypatch)
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 1080, "vcodec": "avc1",
            "duration": 100}
    pp._name(info)
    assert info["mt_suffix"] == " (1080p)" and "__mt_reused" not in info


def test_old_clip_is_not_returned_for_the_whole_video(tmp_path, monkeypatch):
    (tmp_path / "T [a].mp4").write_bytes(b"x")
    pp = name_pp(tmp_path, lambda p: {"height": 1080, "vcodec": "h264", "duration": 3},
                 monkeypatch)
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 1080, "vcodec": "avc1",
            "duration": 600}
    pp._name(info)
    assert info["mt_suffix"] == " (1080p full)"


def test_audio_request_never_reuses_a_video_file(tmp_path, monkeypatch):
    (tmp_path / "T [a].webm").write_bytes(b"x")
    pp = name_pp(tmp_path, lambda p: {"height": 1080, "vcodec": "vp9", "duration": 100},
                 monkeypatch, audio_only=True)
    pp._downloader.params["final_ext"] = "mp3"
    info = {"id": "a", "title": "T", "ext": "webm", "vcodec": "none", "duration": 100}
    pp._name(info)
    assert info["mt_suffix"] == " (audio)"


def test_cut_requests_ignore_length(tmp_path, monkeypatch):
    (tmp_path / "T [a].mp4").write_bytes(b"x")
    pp = name_pp(tmp_path, lambda p: {"height": 144, "vcodec": "h264", "duration": 213},
                 monkeypatch, cuts=True)
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 144, "vcodec": "avc1",
            "duration": 255}
    pp._name(info)
    assert info["__mt_reused"] is True


def test_recode_codec_is_compared(tmp_path, monkeypatch):
    (tmp_path / "T [a].mp4").write_bytes(b"x")
    pp = name_pp(tmp_path, lambda p: {"height": 720, "vcodec": "hevc", "duration": 60},
                 monkeypatch, recode_codec="h264")
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 720, "vcodec": "vp09",
            "duration": 60}
    pp._name(info)
    assert info["mt_suffix"] == " (720p H.264)"


def test_run_reports_the_planned_name(tmp_path, monkeypatch):
    seen = []
    pp = name_pp(tmp_path, lambda p: {}, monkeypatch,
                 on_video=lambda info, planned: seen.append(planned))
    pp.run({"id": "a", "title": "T", "ext": "mp4", "section_start": 5, "section_end": 8})
    assert seen == [str(tmp_path / "T [a] (0.05-0.08).mp4")]


def test_skip_when_reused():
    class P:
        def run(self, info):
            return ["x"], dict(info, ran=True)
    pp = recode.skip_when_reused(P())
    assert pp.run({"__mt_reused": True}) == ([], {"__mt_reused": True})
    assert pp.run({})[1]["ran"] is True


def test_concat_keeps_reused_items(monkeypatch, tmp_path):
    from yt_dlp.postprocessor.ffmpeg import FFmpegConcatPP
    new, old = str(tmp_path / "new.mp4"), str(tmp_path / "old.mp4")
    monkeypatch.setattr(FFmpegConcatPP, "run", lambda self, info: ([new, old], info))
    pp = recode.KeepOldConcatPP.__new__(recode.KeepOldConcatPP)
    pp._progress_hooks = []
    info = {"entries": [{"requested_downloads": [{"filepath": new}]},
                        {"requested_downloads": [{"filepath": old, "__mt_reused": True}]}]}
    assert pp.run(info)[0] == [new]


def test_cover_art_skip_never_fails(monkeypatch):
    notes = []
    pp = recode.SafeEmbedThumbnailPP.__new__(recode.SafeEmbedThumbnailPP)
    pp._on_note = notes.append
    pp._already_have_thumbnail = False
    pp.to_screen = lambda *a, **k: None
    files, info = pp.run.__wrapped__(pp, {"ext": "wav", "thumbnails": []}) \
        if hasattr(pp.run, "__wrapped__") else pp.run({"ext": "wav", "thumbnails": []})
    assert files == [] and notes and "WAV" in notes[0]


# ------------------------------------------------------------ ffmpegtools

def test_encoders_are_proven_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpegtools, "_compiled_in", lambda ff: "h264_nvenc h264_qsv libx264")
    monkeypatch.setattr(ffmpegtools, "test_encode",
                        lambda ff, v, timeout=20.0: calls.append(v) or v != "h264_qsv")
    monkeypatch.setattr(ffmpegtools, "_ffmpeg_key", lambda: ("ffmpeg-a", 1, 1))
    ffmpegtools._cache.clear()
    assert ffmpegtools.available_encoders() == ["h264_nvenc", "libx264"]
    assert ffmpegtools.available_encoders() == ["h264_nvenc", "libx264"]
    assert sorted(calls) == ["h264_nvenc", "h264_qsv", "libx264"]      # tested once
    monkeypatch.setattr(ffmpegtools, "_ffmpeg_key", lambda: ("ffmpeg-b", 2, 2))
    ffmpegtools.available_encoders()                                  # repaired ffmpeg
    assert len(calls) == 6
    ffmpegtools.refresh()
    assert len(calls) == 9
    assert [e["id"] for e in ffmpegtools.encoder_catalog()] == ["h264_nvenc", "libx264"]
    ffmpegtools._cache.clear()


def test_recode_container_and_tags():
    assert ffmpegtools.recode_container("av1_nvenc", "mp4") == "mp4"
    assert ffmpegtools.recode_container("hevc_nvenc", "webm") == "mkv"
    args = ffmpegtools.video_args("hevc_nvenc", "small", "mp4")
    assert args[args.index("-tag:v") + 1] == "hvc1"
    assert "-tag:v" not in ffmpegtools.video_args("h264_nvenc", "small", "mp4")
    assert "-tag:v" not in ffmpegtools.video_args("hevc_nvenc", "small", "mkv")


def test_audio_args_copy_or_encode():
    assert ffmpegtools.audio_args("mp4", "aac") == ["-c:a", "copy"]
    assert ffmpegtools.audio_args("mkv", "opus") == ["-c:a", "copy"]
    small = ffmpegtools.audio_args("mp4", "opus", quality="small")
    assert small == ["-c:a", "aac", "-b:a", "128k"]
    norm = ffmpegtools.audio_args("webm", "opus", normalize=True)
    assert norm[:2] == ["-af", ffmpegtools.LOUDNORM[1]] and "libopus" in norm
    assert "-ar" in norm


# --------------------------------------------------------------- network

@pytest.mark.network
def test_real_clip_download(tmp_path):
    jid = new_job("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    jobs.update(jid, status="running")
    result = media.run_download(jid, "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                                {"quality": "360", "section": "0:10-0:13"})
    files = jobs.get(jid)["files"]
    assert result["saved"] == 1 and len(files) == 1
    assert "(0.10-0.13)" in files[0]["name"]
    facts = ffmpegtools.probe(files[0]["path"])
    assert 2.5 < facts["duration"] < 3.5 and facts["vcodec"] == "h264"
