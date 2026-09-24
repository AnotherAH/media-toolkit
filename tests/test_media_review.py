"""Download engine fixes from the 1.2.0 review: cleanup never deletes a saved
file, playlist failures are charged to the right item on any site, joining a
playlist, the two-pass loudness filter, sound-only files, staging folders and
vertical-video labels. Offline."""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import pytest

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
    def __init__(self, opts, ie, process):
        self.params = opts
        self._ie = ie
        self._process = process

    def in_download_archive(self, info):
        return False

    def extract_info(self, url, download=False, process=True, ie_key=None):
        return self._ie

    def process_ie_result(self, ie, download=True):
        return self._process(self, ie)


def use_fake(monkeypatch, ie, process):
    holder = {}

    @contextlib.contextmanager
    def fake_session(opts):
        holder["ydl"] = FakeYDL(opts, ie, process)
        yield holder["ydl"]

    monkeypatch.setattr(media, "session", fake_session)
    monkeypatch.setattr(media, "_make_pps", lambda ydl, run: None)
    return holder


def moved(ydl, path: Path, vid="a"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    for hook in ydl.params["postprocessor_hooks"]:
        hook({"status": "finished", "postprocessor": "MoveFiles",
              "info_dict": {"id": vid, "filepath": str(path), "__finaldir": str(path.parent),
                            "__files_to_move": {}}})


# ------------------------------------------------------------- clean-up

def test_cleanup_never_deletes_a_saved_file_that_looks_intermediate(monkeypatch, tmp_path):
    """'Report.final.mp4' has the shape of yt-dlp's per-stream names
    ('x.f399.mp4'). A playlist with one failed item cleans up after itself,
    and must not take the saved item with it."""
    dl = tmp_path / "dl"
    saved = dl / "PL" / "Report.final.mp4"

    def process(ydl, ie):
        match = ydl.params["match_filter"]
        match({"id": "a", "title": "Report", "playlist_autonumber": 1, "n_entries": 2},
              incomplete=True)
        ydl.params["progress_hooks"][0]({"status": "downloading", "filename": str(saved),
                                         "tmpfilename": str(saved) + ".part",
                                         "info_dict": {}})
        moved(ydl, saved, "a")
        match({"id": "b", "title": "Gone", "playlist_autonumber": 2, "n_entries": 2},
              incomplete=True)
        ydl.params["logger"].error("ERROR: [youtube] b: Video unavailable")
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "all"})
    assert result["saved"] == 1 and result["failed"] == 1
    assert saved.is_file()


def test_planned_final_name_is_kept_on_cancel(tmp_path):
    jid = new_job()
    o = media.build_download_opts({}, str(tmp_path), None)
    run = media._Run(jid, "u", {}, o, str(tmp_path))
    final = tmp_path / "Notes.fix.mp4"             # an older file with that name
    final.write_bytes(b"old")
    part = tmp_path / "Notes.fix.f137.mp4.part"
    part.write_bytes(b"x")
    run.on_video({"format_id": "137", "vcodec": "avc1"}, str(final))
    run.progress({"status": "downloading", "tmpfilename": str(part), "info_dict": {}})
    run.cleanup_partials()
    assert final.is_file() and not part.exists()


def test_cancel_removes_an_empty_playlist_folder_but_never_the_download_folder(tmp_path):
    jid = new_job()
    dl = tmp_path / "dl"
    folder = dl / "My playlist"
    folder.mkdir(parents=True)
    part = folder / "001 - One [a].f18.mp4.part"
    part.write_bytes(b"x")
    o = media.build_download_opts({}, str(dl), None)
    run = media._Run(jid, "u", {}, o, str(dl))
    run.progress({"status": "downloading", "tmpfilename": str(part), "info_dict": {}})
    run.partials.add(str(dl / "Single [b].f18.mp4.part"))
    run.cleanup_partials()
    assert not folder.exists() and dl.is_dir()


# ---------------------------------------------------- playlist accounting

def test_failure_is_charged_to_the_item_even_without_the_match_filter(monkeypatch):
    """Sites whose extractor cannot tell a video link from a playlist link
    never reach the match filter for an unread entry; yt-dlp still asks the
    archive about every item first."""
    def process(ydl, ie):
        ask = ydl.in_download_archive
        ask({"id": "a", "title": "First", "url": "https://s/a", "playlist_autonumber": 1,
             "n_entries": 2})
        ydl.params["logger"].error("ERROR: [site] a: Video unavailable")
        ask({"id": "b", "title": "Second", "url": "https://s/b", "playlist_autonumber": 2,
             "n_entries": 2})
        ydl.params["logger"].error("ERROR: [site] b: HTTP Error 403: Forbidden")
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    # Nothing saved: the job fails with the first item's own error.
    with pytest.raises(RuntimeError, match="unavailable"):
        media.run_download(new_job(), "u", {"playlist_mode": "all"})


def test_both_failures_are_listed(monkeypatch, tmp_path):
    def process(ydl, ie):
        ask = ydl.in_download_archive
        ask({"id": "a", "title": "First", "url": "https://s/a", "playlist_autonumber": 1,
             "n_entries": 3})
        ydl.params["logger"].error("ERROR: [site] a: Video unavailable")
        ask({"id": "b", "title": "Second", "url": "https://s/b", "playlist_autonumber": 2,
             "n_entries": 3})
        ydl.params["logger"].error("ERROR: [site] b: Private video")
        ask({"id": "c", "title": "Third", "url": "https://s/c", "playlist_autonumber": 3,
             "n_entries": 3})
        moved(ydl, tmp_path / "dl" / "PL" / "003 - Third [c].mp4", "c")
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    result = media.run_download(new_job(), "u", {"playlist_mode": "all"})
    assert result["saved"] == 1 and result["failed"] == 2
    assert [f["title"] for f in result["items_failed"]] == ["First", "Second"]


def test_cancel_stops_before_the_next_item_on_any_site(monkeypatch, tmp_path):
    jid = new_job()
    asked = []

    def process(ydl, ie):
        for n in (1, 2, 3):
            ydl.in_download_archive({"id": str(n), "playlist_autonumber": n, "n_entries": 3})
            asked.append(n)
            if n == 1:
                jobs.cancel(jid)
        return {"_type": "playlist", "entries": []}

    use_fake(monkeypatch, {"_type": "playlist", "id": "PL", "title": "PL"}, process)
    with pytest.raises(jobs.Cancelled):
        media.run_download(jid, "u", {"playlist_mode": "all"})
    assert asked == [1]


# ------------------------------------------------------------ staging

def test_same_link_twice_gets_two_staging_folders():
    a = media.claim_staging("C:/t/dl-abc", "j1")
    b = media.claim_staging("C:/t/dl-abc", "j2")
    assert a == "C:/t/dl-abc" and b == "C:/t/dl-abc-j2"
    assert media.claim_staging("C:/t/dl-abc", "j1") == "C:/t/dl-abc"    # a retry of j1
    media.release_staging(a)
    media.release_staging(b)
    assert media.claim_staging("C:/t/dl-abc", "j3") == "C:/t/dl-abc"
    media.release_staging("C:/t/dl-abc")


def test_old_staging_folders_are_pruned(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "temp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(media, "_staging_pruned", 0.0)
    old, fresh, busy = tmp_path / "dl-old", tmp_path / "dl-new", tmp_path / "dl-busy"
    for d in (old, fresh, busy):
        d.mkdir()
        (d / "x.part").write_bytes(b"x")
    past = time.time() - 5 * 24 * 3600
    for d in (old, busy):
        os.utime(d / "x.part", (past, past))
        os.utime(d, (past, past))
    media.claim_staging(str(busy), "j9")
    try:
        media._prune_staging()
    finally:
        media.release_staging(str(busy))
    assert not old.exists() and fresh.exists() and busy.exists()


# ------------------------------------------------------------- recode.py

def test_concat_keeps_its_own_key_and_never_moves_a_single_item(tmp_path):
    assert recode.KeepOldConcatPP.pp_key() == "Concat"
    assert media.pp_stage("Concat") == "Joining the videos into one file…"
    pp = recode.KeepOldConcatPP.__new__(recode.KeepOldConcatPP)
    only = tmp_path / "001 - old [a].mp4"
    only.write_bytes(b"x")
    assert pp.concat_files([str(only)], str(tmp_path / "joined.mp4")) == []
    assert only.is_file() and not (tmp_path / "joined.mp4").exists()


def test_recode_leaves_sound_only_files_alone(monkeypatch, tmp_path):
    path = tmp_path / "show.m4a"
    path.write_bytes(b"x")
    monkeypatch.setattr(ffmpegtools, "probe", lambda p: {"vcodec": "", "acodec": "aac",
                                                         "duration": 60})
    pp = recode.ForceRecodePP.__new__(recode.ForceRecodePP)
    pp._progress_hooks = []
    pp.to_screen = lambda *a, **k: None
    pp.run_ffmpeg = lambda *a, **k: pytest.fail("must not encode")
    files, info = pp.run.__wrapped__(pp, {"filepath": str(path), "ext": "m4a"})
    assert files == [] and info["filepath"] == str(path)


def test_vertical_video_is_named_by_its_smaller_side(tmp_path, monkeypatch):
    (tmp_path / "T [a].mp4").write_bytes(b"x")
    monkeypatch.setattr(ffmpegtools, "probe",
                        lambda p: {"height": 1280, "width": 720, "vcodec": "h264", "duration": 30})

    class D:
        params = {"final_ext": None}

        def prepare_filename(self, info, *a, **k):
            return str(tmp_path / f"T [a]{info.get('mt_suffix', '')}.{info['ext']}")

    pp = recode.RequestNamePP(None)
    pp._downloader = D()
    pp.to_screen = lambda *a, **k: None
    info = {"id": "a", "title": "T", "ext": "mp4", "height": 1920, "width": 1080,
            "vcodec": "avc1", "duration": 30}
    pp._name(info)
    assert info["mt_suffix"] == " (1080p)"


def test_format_label_uses_the_smaller_side():
    rows = media._format_rows({"duration": 60, "formats": [
        {"format_id": "137", "width": 1080, "height": 1920, "vcodec": "avc1", "acodec": "none",
         "filesize": 1000, "protocol": "https", "ext": "mp4"}]})
    assert rows[0]["label"].startswith("1080p ·")


def test_video_summary_says_it_is_not_a_channel():
    assert media.summarize({"id": "a", "title": "T", "formats": []})["is_channel"] is False


# ------------------------------------------------------- ffmpegtools.py

@pytest.mark.parametrize("container, codec", [
    ("mp3", "libmp3lame"), ("flac", "flac"), ("wav", "pcm_s16le"), ("opus", "libopus"),
    ("ogg", "libopus"), ("m4a", "aac"), ("mp4", "aac"), ("webm", "libopus")])
def test_levelling_sound_only_files_keeps_a_codec_their_container_takes(container, codec):
    args = ffmpegtools.audio_args(container, "mp3", normalize=True)
    assert args[args.index("-c:a") + 1] == codec


LOUDNORM_LOG = """[Parsed_loudnorm_0 @ 0000018b9b211400]
{
	"input_i" : "-29.44",
	"input_tp" : "-12.82",
	"input_lra" : "14.90",
	"input_thresh" : "-40.86",
	"output_i" : "-16.67",
	"output_tp" : "-1.50",
	"output_lra" : "10.60",
	"output_thresh" : "-27.09",
	"normalization_type" : "dynamic",
	"target_offset" : "2.67"
}
[out#0/null @ 0000018b9b211400] video:0KiB audio:15000KiB
"""


def test_two_pass_loudness_uses_the_measurements():
    measured = ffmpegtools.parse_loudness(LOUDNORM_LOG)
    assert measured["input_i"] == -29.44 and measured["target_offset"] == 2.67
    args = ffmpegtools.loudnorm_args(measured)
    assert args[0] == "-af" and "measured_I=-29.44" in args[1] and "offset=2.67" in args[1]
    assert "linear=true" in args[1] and args[-2:] == ["-ar", "48000"]
    norm = ffmpegtools.audio_args("mp4", "aac", normalize=True, loudness=measured)
    assert "measured_I=-29.44" in norm[1]


def test_unmeasurable_sound_falls_back_to_one_pass():
    silent = LOUDNORM_LOG.replace('"-29.44"', '"-inf"')
    assert ffmpegtools.parse_loudness(silent) is None
    assert ffmpegtools.parse_loudness("no json here") is None
    assert ffmpegtools.loudnorm_args(None) == ffmpegtools.LOUDNORM


def test_measure_loudness_reads_the_scan(monkeypatch):
    pp = recode.NormalizeAudioPP.__new__(recode.NormalizeAudioPP)
    pp._should_stop = None
    pp.write_debug = lambda *a, **k: None
    monkeypatch.setattr(recode.NormalizeAudioPP, "executable", "ffmpeg", raising=False)
    seen = {}

    def fake_run(cmd, outputs=()):
        seen["cmd"] = cmd
        return 0, LOUDNORM_LOG
    pp._run_stoppable = fake_run
    assert pp.measure_loudness("C:/x/a.mp4")["input_lra"] == 14.9
    assert "print_format=json" in " ".join(seen["cmd"]) and seen["cmd"][-1] == "-"
    pp._run_stoppable = lambda cmd, outputs=(): (1, "error")
    assert pp.measure_loudness("C:/x/a.mp4") is None
