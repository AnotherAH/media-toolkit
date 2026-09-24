"""Live recording: command building, naming, waiting and the recording loop.

Everything here is offline. The ffmpeg tests use the bundled bin/ffmpeg with
synthetic sources (lavfi, or small files made on the spot) and are skipped
when it is not there.
"""
from __future__ import annotations

import itertools
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import config, errors, live

FFMPEG = config.ffmpeg_dir()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="bin/ffmpeg not available")


# ---------------------------------------------------------------- fixtures

class FakeJobs:
    """Just the part of the jobs contract live.py uses."""

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.fields: dict = {}
        self.updates: list[dict] = []
        self.files: list[tuple[str, str, str]] = []
        self.stop = threading.Event()

    def update(self, jid, **fields):
        self.fields.update(fields)
        self.updates.append(dict(fields))

    def add_file(self, jid, path, kind="", label=""):
        self.files.append((path, kind, label))

    def cancelled(self, jid):
        return bool(jid) and self.stop.is_set()

    def raise_if_cancelled(self, jid):
        if self.cancelled(jid):
            raise self.Cancelled()


@pytest.fixture
def fake_jobs(monkeypatch):
    fj = FakeJobs()
    monkeypatch.setattr(live, "jobs", fj)
    return fj


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "downloads"
    d.mkdir()
    cfg = dict(config.get(), download_dir=str(d), proxy="", restrict_filenames=False)
    monkeypatch.setattr(live.config, "get", lambda: dict(cfg))
    return d


def _ff(*args, timeout=120):
    exe = str(Path(FFMPEG) / "ffmpeg.exe") if FFMPEG and (Path(FFMPEG) / "ffmpeg.exe").exists() \
        else str(Path(FFMPEG or ".") / "ffmpeg")
    subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    """Small source files, made once: a 130 s video+AAC stream, and 3 s sound
    only files in AAC, MP3 and Opus, all in MPEG-TS like a recording."""
    if not FFMPEG:
        pytest.skip("bin/ffmpeg not available")
    d = tmp_path_factory.mktemp("media")
    _ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=22050", "-t", "130", "-c:v", "libx264",
        "-preset", "ultrafast", "-g", "20", "-c:a", "aac", "-b:a", "32k",
        "-f", "mpegts", str(d / "long.ts"))
    for codec, name in (("aac", "aac.ts"), ("libmp3lame", "mp3.ts"), ("libopus", "opus.ts")):
        _ff("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
            "-c:a", codec, "-f", "mpegts", str(d / name))
    _ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi",
        "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-f", "mpegts", str(d / "av.ts"))
    return d


def _target(url="https://example.com/v.m3u8", audio=None, **kw):
    video = {"url": url, "headers": {"User-Agent": "UA/1", "Referer": "https://example.com/"},
             "protocol": "m3u8_native", "height": 720, "format_id": "95", "vcodec": "avc1.4d401f",
             "acodec": "mp4a.40.2", "cookies": ""}
    video.update(kw)
    return {"video": video, "audio": audio, "single": audio is None, "is_live": True,
            "live_status": "is_live", "title": "Test", "uploader": "NASA", "thumbnail": "",
            "release_timestamp": None, "duration": 0, "meta": {"title": "Test"}}


# ------------------------------------------------------------ option parsing

@pytest.mark.parametrize("value, seconds", [
    (None, None), ("", None), (0, None), ("0", None), (-3, None), ("abc", None),
    (float("nan"), None), (float("inf"), None), (True, None),
    (0.5, 60), (1, 60), (1.5, 90), ("1.5", 90), ("1,5", 90), (" 2 ", 120), (10, 600),
])
def test_split_minutes_are_normalised_once(value, seconds):
    assert live.split_seconds({"split_minutes": value}) == seconds


@pytest.mark.parametrize("value, seconds", [
    (None, None), ("", None), (0, None), (-1, None), ("never", None),
    (0.25, 15), (1.5, 90), ("2", 120), (0.001, 1), (1e300, 31 * 86400),
])
def test_max_minutes_accepts_fractions(value, seconds):
    assert live.limit_seconds({"max_minutes": value}) == seconds


def test_a_huge_limit_still_gives_ffmpeg_a_readable_time(tmp_path):
    cmd = live.build_command(_target(), tmp_path / "a.ts", {"max_minutes": "1e300",
                                                             "split_minutes": 1e300})
    assert cmd[cmd.index("-t") + 1] == str(31 * 86400)
    assert cmd[cmd.index("-segment_time") + 1] == str(31 * 86400)


def test_wait_minutes_default_and_bounds():
    assert live.wait_seconds({}) == 180 * 60
    assert live.wait_seconds({"wait_minutes": 0}) == 180 * 60
    assert live.wait_seconds({"wait_minutes": 90}) == 90 * 60
    assert live.wait_seconds({"wait_minutes": 0.1}) == 60
    assert live.wait_seconds({"wait_minutes": 10 ** 9}) == 14 * 86400


@pytest.mark.parametrize("value, audio_only, expected", [
    ("mp4", False, "mp4"), ("", False, "mp4"), (None, False, "mp4"), ("mkv", False, "mkv"),
    ("ts", False, "ts"), ("webm", False, "mp4"), ("m4a", True, "m4a"), ("mp4", True, "m4a"),
    ("ts", True, "ts"), ("mkv", True, "mka"), ("", True, "m4a"), ("TS", False, "ts"),
])
def test_container_choice(value, audio_only, expected):
    assert live._container(value, audio_only) == expected


# ------------------------------------------------------------ build_command

LIMITS = [None, 1.5]
SPLITS = [None, 2]
SHAPES = ["separate", "muxed", "audio"]
EXTS = [".ts", ".mkv"]


def _shape(shape):
    if shape == "separate":
        return _target(audio={"url": "https://example.com/a.m3u8", "headers": {},
                              "protocol": "m3u8_native", "acodec": "mp4a.40.2", "vcodec": "none",
                              "cookies": "SID=1; path=/; domain=.example.com;\r\n"})
    if shape == "muxed":
        return _target()
    t = _target()
    t["audio"], t["video"] = t["video"], None
    return t


@pytest.mark.parametrize("shape, limit, split, ext",
                         list(itertools.product(SHAPES, LIMITS, SPLITS, EXTS)))
def test_build_command_every_combination(tmp_path, shape, limit, split, ext):
    o = {"max_minutes": limit, "split_minutes": split}
    out = tmp_path / f"Title 2026-09-23 18-51-00{ext}"
    cmd = live.build_command(_shape(shape), out, o, part_start=3)

    assert Path(cmd[0]).name.startswith("ffmpeg")
    assert "-y" not in cmd and "-n" in cmd                 # never overwrite anything
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"
    assert cmd.count("-i") == (2 if shape == "separate" else 1)
    assert cmd[cmd.index("-c") + 1] == "copy"

    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == {"separate": ["0:v:0", "1:a:0"], "muxed": ["0:v:0?", "0:a:0?"],
                    "audio": ["0:a:0"]}[shape]

    if limit:
        assert cmd[cmd.index("-t") + 1] == "90"
    else:
        assert "-t" not in cmd

    fmt = "matroska" if ext == ".mkv" else "mpegts"
    if split:
        i = cmd.index("-f")
        assert cmd[i + 1] == "segment"
        assert cmd[cmd.index("-segment_time") + 1] == "120"
        assert cmd[cmd.index("-segment_start_number") + 1] == "3"
        assert cmd[cmd.index("-segment_format") + 1] == fmt
        assert cmd[-1] == str(tmp_path / f"Title 2026-09-23 18-51-00 part%03d{ext}")
    else:
        assert cmd[-3:] == ["-f", fmt, str(out)]
        assert "-segment_time" not in cmd

    # HTTP input options, and HLS options only on m3u8 inputs
    assert cmd.count("-reconnect") == cmd.count("-i")
    assert cmd.count("-seg_max_retry") == cmd.count("-i")
    assert cmd[cmd.index("-user_agent") + 1] == "UA/1"
    if shape == "separate":
        assert cmd[cmd.index("-cookies") + 1].startswith("SID=1")
    else:
        assert "-cookies" not in cmd


def test_headers_are_one_crlf_blob_without_user_agent(tmp_path):
    cmd = live.build_command(_target(), tmp_path / "a.ts", {})
    blob = cmd[cmd.index("-headers") + 1]
    assert blob == "Referer: https://example.com/\r\n"
    assert cmd[cmd.index("-user_agent") + 1] == "UA/1"


def test_non_http_inputs_get_no_http_options(tmp_path):
    """ffmpeg refuses to start when an input option matches nothing."""
    t = _target(url="rtmp://example.com/live/key", protocol="rtmp")
    cmd = live.build_command(t, tmp_path / "a.ts", {})
    for opt in ("-reconnect", "-rw_timeout", "-seg_max_retry", "-headers", "-user_agent"):
        assert opt not in cmd
    t = _target(url="https://radio.example.com/stream.mp3", protocol="https")
    cmd = live.build_command(t, tmp_path / "a.ts", {})
    assert "-reconnect" in cmd and "-seg_max_retry" not in cmd


def test_split_pattern_escapes_percent(tmp_path):
    out = tmp_path / "100% Live Radio 50%off 2026-09-23 18-55-00.ts"
    cmd = live.build_command(_target(), out, {"split_minutes": 1})
    assert cmd[-1].endswith("100%% Live Radio 50%%off 2026-09-23 18-55-00 part%03d.ts")
    # A single file is not a pattern and keeps its name as is.
    cmd = live.build_command(_target(), out, {})
    assert cmd[-1] == str(out)


def test_remaining_overrides_the_limit(tmp_path):
    cmd = live.build_command(_target(), tmp_path / "a.ts", {"max_minutes": 10}, remaining=42.5)
    assert cmd[cmd.index("-t") + 1] == "42.5"
    cmd = live.build_command(_target(), tmp_path / "a.ts", {"max_minutes": 10}, remaining=0.2)
    assert cmd[cmd.index("-t") + 1] == "1"


def test_fractional_split_below_one_minute_still_splits(tmp_path):
    """0.5 used to become int(0) and record a single file that was then lost."""
    cmd = live.build_command(_target(), tmp_path / "a.ts", {"split_minutes": 0.5})
    assert cmd[cmd.index("-segment_time") + 1] == "60"


@pytest.mark.parametrize("vcodec, acodec, ext", [
    ("avc1.64001F", "mp4a.40.2", "ts"), (None, None, "ts"), ("", "", "ts"),
    ("hev1", "ec-3", "ts"), ("avc1", "opus", "ts"), ("vp09.00.40.08", "opus", "mkv"),
    ("av01.0.08M.08", "mp4a.40.2", "mkv"), ("none", "vorbis", "mkv"),
])
def test_record_container_follows_codecs(vcodec, acodec, ext):
    assert live.record_ext(_target(vcodec=vcodec, acodec=acodec)) == ext


@pytest.mark.parametrize("streams, container, src, expected", [
    ([("Video", "h264"), ("Audio", "aac")], "mp4", "ts", "mp4"),
    ([("Video", "h264"), ("Audio", "mp3")], "mp4", "ts", "mp4"),
    ([("Video", "h264"), ("Audio", "vorbis")], "mp4", "ts", "mkv"),
    ([("Video", "vp9"), ("Audio", "opus")], "mp4", "mkv", "mp4"),
    ([("Video", "h264"), ("Audio", "aac")], "mkv", "ts", "mkv"),
    ([("Video", "h264"), ("Audio", "aac")], "ts", "ts", "ts"),
    ([("Video", "vp9"), ("Audio", "opus")], "ts", "mkv", "mkv"),
    ([("Audio", "aac")], "m4a", "ts", "m4a"),
    ([("Audio", "aac")], "mp4", "ts", "m4a"),        # video asked, stream had no picture
    ([("Audio", "mp3")], "m4a", "ts", "mp3"),
    ([("Audio", "opus")], "m4a", "ts", "mka"),
    ([("Audio", "aac")], "mka", "ts", "mka"),
    ([("Audio", "mp3"), ("Audio", "mp3")], "m4a", "ts", "mka"),
    ([], "mp4", "ts", "ts"),                          # unreadable: keep as recorded
])
def test_final_container_from_real_streams(streams, container, src, expected):
    assert live.final_ext(streams, container, src) == expected


# ------------------------------------------------------------- names, titles

@pytest.mark.parametrize("title, clean", [
    ("NASA Live: Official Stream of NASA TV 2026-09-23 18:51", "NASA Live: Official Stream of NASA TV"),
    ("Radio 2026-09-23 18:51  ", "Radio"),
    ("Launch 2026-09-23", "Launch 2026-09-23"),
    ("2026-09-23 18:51", "2026-09-23 18:51"),        # nothing left: keep it
    ("پخش زنده 2026-09-23 18:51", "پخش زنده"),
    ("", ""), (None, ""),
])
def test_clean_title_drops_the_live_stamp(title, clean):
    assert live.clean_title(title) == clean


def test_base_stem_has_seconds_and_safe_characters():
    when = time.mktime((2026, 9, 23, 18, 51, 7, 0, 0, -1))
    assert live.base_stem('A/B: "live"? 2026-09-23 18:51', when) == "A-B- -live 2026-09-23 18-51-07"
    assert live.base_stem("", when) == "Live recording 2026-09-23 18-51-07"
    assert live.base_stem("پخش زنده", when) == "پخش زنده 2026-09-23 18-51-07"
    assert live.base_stem("پخش زنده", when, restricted=True) == "live_recording_2026-09-23_18-51-07"
    assert live.base_stem("100% Radio", when) == "100% Radio 2026-09-23 18-51-07"
    long = live.base_stem("x" * 400, when)
    assert long.endswith(" 2026-09-23 18-51-07") and len(long) <= 121


def test_a_deep_folder_keeps_the_whole_path_under_the_windows_limit(tmp_path):
    deep = Path("C:/") / ("d" * 150)
    stem = live.base_stem("x" * 400, time.time(), room=live._title_room(deep))
    worst = deep / f"{stem} part001 (2).mkv"
    assert len(str(worst)) <= 259 and stem.startswith("x" * 20)
    assert live._title_room(tmp_path / "short") == 100 or len(str(tmp_path)) > 100


def test_claim_stem_is_unique_on_disk_and_between_running_jobs(tmp_path):
    base = "NASA 2026-09-23 18-51-07"
    a = live.claim_stem(tmp_path, base)
    b = live.claim_stem(tmp_path, base)                 # same stream, same second
    assert (a, b) == (base, base + " (2)")
    live.release_stem(tmp_path, a)
    live.release_stem(tmp_path, b)

    (tmp_path / f"{base}.mp4").write_bytes(b"x")         # an earlier recording
    (tmp_path / f"{base} (2) part001.ts").write_bytes(b"x")
    c = live.claim_stem(tmp_path, base)
    assert c == base + " (3)"
    live.release_stem(tmp_path, c)
    # Case-insensitive, like the file system.
    (tmp_path / "radio 2026-09-23 18-51-07.M4A").write_bytes(b"x")
    d = live.claim_stem(tmp_path, "Radio 2026-09-23 18-51-07")
    assert d == "Radio 2026-09-23 18-51-07 (2)"
    live.release_stem(tmp_path, d)


@pytest.mark.parametrize("url, name", [
    ("https://www.youtube.com/@NASA/live", "NASA"),
    ("https://youtube.com/@%D9%86%D8%A7%D8%B3%D8%A7/live", "ناسا"),
    ("https://www.twitch.tv/somestreamer", "somestreamer"),
    ("https://kick.com/someone", "someone"),
    ("https://www.tiktok.com/@creator/live", "creator"),
    ("https://www.youtube.com/channel/UC123/live", ""),
    ("https://www.twitch.tv/videos/123", ""),
    ("not a url", ""),
])
def test_channel_from_url(url, name):
    assert live._channel_from_url(url) == name


@pytest.mark.parametrize("url, out", [
    ("https://www.youtube.com/@NASA", "https://www.youtube.com/@NASA/live"),
    ("https://www.youtube.com/@NASA/", "https://www.youtube.com/@NASA/live"),
    ("https://youtube.com/channel/UC1?si=x", "https://youtube.com/channel/UC1/live?si=x"),
    ("https://www.youtube.com/@NASA/live", "https://www.youtube.com/@NASA/live"),
    ("https://www.youtube.com/@NASA/streams", "https://www.youtube.com/@NASA/streams"),
    ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
    ("https://www.twitch.tv/name", "https://www.twitch.tv/name"),
    ("https://notyoutube.com/@x", "https://notyoutube.com/@x"),
])
def test_bare_youtube_channel_means_its_live_page(url, out):
    assert live.live_url(url) == out


@pytest.mark.parametrize("target, regular", [
    ({"is_live": True, "live_status": "is_live"}, False),
    ({"is_live": False, "live_status": "not_live"}, True),
    ({"is_live": False, "live_status": "was_live"}, True),
    ({"is_live": False, "live_status": "post_live"}, True),
    ({"is_live": False, "live_status": "", "duration": 0}, False),     # radio
    ({"is_live": False, "live_status": "", "duration": 596}, True),    # a video
    ({"is_live": False, "live_status": "is_upcoming"}, False),
])
def test_is_regular(target, regular):
    assert live._is_regular(target) is regular


# ----------------------------------------------------- not live / waiting

def test_not_live_detection(monkeypatch):
    monkeypatch.setattr(live, "_upcoming_meta", lambda url: {})
    nl = live._not_live_from("https://www.youtube.com/@NASA/live",
                             Exception("ERROR: [youtube:tab] UC1: The channel is not currently live"), True)
    assert nl and not nl.upcoming and nl.meta["uploader"] == "NASA"
    assert nl.reason == "The channel is not currently live"

    nl = live._not_live_from("u", Exception("ERROR: [youtube] abc: This live event will begin in 8 hours."), True)
    assert nl and nl.upcoming and nl.reason == "This live event will begin in 8 hours."
    nl = live._not_live_from("u", Exception("Premieres in 3 hours"), False)
    assert nl and nl.upcoming

    assert live._not_live_from("u", Exception("ERROR: [youtube] abc: Video unavailable"), True) is None
    assert live._not_live_from("u", Exception("HTTP Error 404: Not Found"), True) is None
    # "No formats" is only "not live yet" when the video says it is upcoming.
    fmt = Exception("Requested format is not available. Use --list-formats")
    assert live._not_live_from("u", fmt, True) is None
    monkeypatch.setattr(live, "_upcoming_meta", lambda url: {
        "live_status": "is_upcoming", "release_timestamp": 1_900_000_000, "uploader": "NASA",
        "title": "Launch", "thumbnail": "t.jpg"})
    nl = live._not_live_from("u", fmt, True)
    assert nl and nl.upcoming and nl.meta["release_timestamp"] == 1_900_000_000


def test_no_formats_is_looked_at_even_on_a_quick_poll(monkeypatch):
    """Waiting skips the extra lookup on most polls. A bare 'no formats'
    answer cannot be told apart without it, and failing there ended a wait on
    its second check."""
    looked = []
    monkeypatch.setattr(live, "_upcoming_meta", lambda url: looked.append(url) or {
        "live_status": "is_upcoming", "release_timestamp": 1_900_000_000})
    nl = live._not_live_from("u", Exception("ERROR: [youtube] abc: No video formats found!"), False)
    assert nl and nl.upcoming and looked == ["u"]
    # A plain "will begin" needs no second look on a quick poll.
    assert live._not_live_from("u", Exception("This live event will begin in 3 hours"), False)
    assert looked == ["u"]


class _FakeYDL:
    def __init__(self, result):
        self.result = result

    def extract_info(self, url, download=False):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _fake_ydl(monkeypatch, result, seen=None):
    import contextlib

    @contextlib.contextmanager
    def fake(opts):
        if seen is not None:
            seen.append(opts)
        yield _FakeYDL(result)

    monkeypatch.setattr(live, "_ydl", fake)
    monkeypatch.setattr(live.media, "base_opts", lambda: {})


def test_resolve_passes_quality_and_audio_choice(monkeypatch):
    seen = []
    info = {"title": "NASA Live 2026-09-23 18:51", "is_live": True, "live_status": "is_live",
            "uploader": "NASA", "url": "https://x/a.m3u8", "height": 720, "vcodec": "avc1",
            "acodec": "mp4a", "protocol": "m3u8_native", "formats": []}
    _fake_ydl(monkeypatch, info, seen)
    t = live.resolve("https://www.youtube.com/@NASA", "480", False)
    assert "height<=480" in seen[0]["format"]
    assert seen[0]["playlist_items"] == "1" and seen[0]["noplaylist"]
    assert t["title"] == "NASA Live" and t["uploader"] == "NASA" and t["is_live"]
    assert t["video"]["url"] == "https://x/a.m3u8" and t["single"]
    live.resolve("https://www.youtube.com/@NASA", "480", True)
    assert seen[-1]["format"].startswith("ba/")


@pytest.mark.parametrize("url", ["file:///C:/Users/me/secret.mp4", "C:\\x.ts",
                                 "concat:a.ts|b.ts", "subfile,,start,0,end,0,,:x.ts", ""])
def test_resolve_never_hands_ffmpeg_a_local_or_special_url(monkeypatch, url):
    info = {"title": "x", "is_live": True, "live_status": "is_live", "url": url,
            "vcodec": "avc1", "acodec": "mp4a", "height": 720, "protocol": "m3u8_native"}
    _fake_ydl(monkeypatch, info)
    with pytest.raises(RuntimeError, match="No playable stream"):
        live.resolve("https://evil.example/live")


def test_resolve_upcoming_info_is_not_live_yet(monkeypatch):
    info = {"title": "Launch", "live_status": "is_upcoming", "release_timestamp": 1_900_000_000,
            "uploader": "NASA"}
    _fake_ydl(monkeypatch, info)
    with pytest.raises(live.NotLiveYet) as e:
        live.resolve("https://www.youtube.com/watch?v=abc")
    assert e.value.upcoming and e.value.meta["release_timestamp"] == 1_900_000_000


def test_check_answers_instead_of_failing(monkeypatch):
    def offline(url, quality="best", audio_only=False, **kw):
        raise live.NotLiveYet("The channel is not currently live", {"uploader": "NASA"})
    monkeypatch.setattr(live, "resolve", offline)
    r = live.check("https://www.youtube.com/@NASA/live")
    assert r["offline"] and not r["is_live"] and not r["upcoming"] and r["uploader"] == "NASA"
    assert r["site"] == "YouTube"
    assert set(r) >= {"is_live", "offline", "upcoming", "title", "uploader", "thumbnail", "height",
                      "has_video", "has_audio", "release_timestamp", "site"}

    t = _target()
    t.update(is_live=False, live_status="not_live", duration=600, title="Big Buck Bunny")
    monkeypatch.setattr(live, "resolve", lambda *a, **k: t)
    r = live.check("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert r["regular"] and not r["is_live"] and not r["offline"]

    t2 = _target()
    seen = []
    monkeypatch.setattr(live, "resolve", lambda url, q="best", a=False, **k: seen.append((q, a)) or t2)
    r = live.check("https://x", "720", True)
    assert r["is_live"] and r["has_video"] and r["has_audio"] and r["height"] == 0
    assert seen == [("720", False)]                 # track facts come from the video resolve
    assert live.check("https://x", "720", False)["height"] == 720


class Clock:
    """time.time/time.sleep that advance instantly."""

    def __init__(self, now=1_000_000.0):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_wait_keeps_waiting_until_the_deadline(monkeypatch, fake_jobs):
    clock = Clock()
    monkeypatch.setattr(live.time, "time", clock.time)
    monkeypatch.setattr(live.time, "sleep", clock.sleep)
    calls = []

    def fake_resolve(url, quality="best", audio_only=False, details=True):
        calls.append((quality, audio_only))
        raise live.NotLiveYet("This live event will begin in 8 hours.", {"uploader": "NASA"}, True)

    monkeypatch.setattr(live, "resolve", fake_resolve)
    deadline = clock.now + 180 * 60
    with pytest.raises(errors.AppError) as e:
        live.wait_until_live("https://www.youtube.com/watch?v=abc", "j1", "720", True, deadline)
    assert e.value.code == "live_not_live"
    assert clock.now >= deadline                      # waited the whole 3 hours, not 6 minutes
    assert len(calls) > 100 and set(calls) == {("720", True)}
    assert fake_jobs.fields["give_up_at"] == deadline
    assert fake_jobs.fields["live_phase"] == "waiting"
    assert max(b - a for a, b in zip([0] + clock.slept, clock.slept)) <= 90


def test_wait_sleeps_until_just_before_a_known_start(monkeypatch, fake_jobs):
    clock = Clock()
    monkeypatch.setattr(live.time, "time", clock.time)
    monkeypatch.setattr(live.time, "sleep", clock.sleep)
    start = clock.now + 3600
    n = {"calls": 0}
    target = _target()

    def fake_resolve(url, quality="best", audio_only=False, details=True):
        n["calls"] += 1
        if clock.now < start:
            raise live.NotLiveYet("will begin in 1 hour", {"release_timestamp": start}, True)
        return target

    monkeypatch.setattr(live, "resolve", fake_resolve)
    got = live.wait_until_live("u", "j1", deadline=clock.now + 3 * 3600)
    assert got is target
    assert n["calls"] <= 10                           # slept, did not poll all hour
    checks = [u["next_check_at"] for u in fake_jobs.updates if u.get("next_check_at")]
    assert start - 60 in checks


def test_wait_fails_fast_on_a_real_error(monkeypatch, fake_jobs):
    def fake_resolve(url, quality="best", audio_only=False, details=True):
        raise Exception("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")
    monkeypatch.setattr(live, "resolve", fake_resolve)
    with pytest.raises(Exception, match="Private video"):
        live.wait_until_live("u", "j1", deadline=time.time() + 3600)


def test_wait_stops_when_cancelled(monkeypatch, fake_jobs):
    def fake_resolve(url, quality="best", audio_only=False, details=True):
        raise live.NotLiveYet("The channel is not currently live")
    monkeypatch.setattr(live, "resolve", fake_resolve)
    threading.Timer(0.3, fake_jobs.stop.set).start()
    t0 = time.monotonic()
    with pytest.raises(FakeJobs.Cancelled):
        live.wait_until_live("u", "j1", deadline=time.time() + 3600)
    assert time.monotonic() - t0 < 3


# ------------------------------------------------------------ run_live paths

def test_run_live_refuses_a_regular_video(monkeypatch, fake_jobs, outdir):
    t = _target()
    t.update(is_live=False, live_status="not_live", duration=596, title="Big Buck Bunny",
             uploader="Blender", thumbnail="https://i.ytimg.com/bbb.jpg")
    monkeypatch.setattr(live, "resolve", lambda *a, **k: t)
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", {})
    err = errors.classify(e.value, "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "live")
    assert err["code"] == "live_not_live"
    assert err.get("params", {}).get("title") == "Big Buck Bunny"
    assert err["params"]["live_status"] == "not_live"
    assert fake_jobs.fields["thumbnail"] == "https://i.ytimg.com/bbb.jpg"
    assert not list(outdir.iterdir())


def test_run_live_offline_channel_names_it(monkeypatch, fake_jobs, outdir):
    def offline(*a, **k):
        raise live.NotLiveYet("The channel is not currently live", {"uploader": "NASA"})
    monkeypatch.setattr(live, "resolve", offline)
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "https://www.youtube.com/@NASA/live", {})
    err = errors.classify(e.value, "https://www.youtube.com/@NASA/live", "live")
    assert err["code"] == "live_not_live" and err["title"] == "NASA isn't live right now"


def test_run_live_ignores_output_dir_from_the_request(monkeypatch, fake_jobs, outdir, tmp_path):
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _target())
    fake_jobs.stop.set()                               # stop before recording starts
    with pytest.raises(FakeJobs.Cancelled):
        live.run_live("j1", "u", {"output_dir": str(tmp_path / "evil")})
    assert not (tmp_path / "evil").exists()


def test_stop_during_startup_records_nothing(monkeypatch, fake_jobs, outdir):
    """LIVE-11: Stop while the stream is being looked up is a cancel, not a clip."""
    def slow_resolve(*a, **k):
        fake_jobs.stop.set()
        return _target()
    monkeypatch.setattr(live, "resolve", slow_resolve)
    ran = []
    monkeypatch.setattr(live, "_run_ffmpeg", lambda *a, **k: ran.append(1))
    with pytest.raises(FakeJobs.Cancelled):
        live.run_live("j1", "u", {})
    assert not ran and not fake_jobs.files


def _file_target(path: Path, title="Test Stream", audio_only=False):
    """A 'stream' that is really a local file: ffmpeg reads it at full speed
    and ends cleanly, like a stream that finished."""
    t = _target(url=str(path), protocol="file", headers={})
    t.update(title=title, is_live=False, live_status="", meta={"title": title})
    if audio_only:
        t["audio"], t["video"] = t["video"], None
    return t


@needs_ffmpeg
def test_split_recording_is_complete_and_numbered(monkeypatch, fake_jobs, outdir, media_dir):
    """LIVE-1: split recordings used to stop after about 25 seconds."""
    title = "100% Live Radio 2026-09-23 18:55"
    t = _file_target(media_dir / "long.ts", title)
    monkeypatch.setattr(live, "resolve", lambda *a, **k: t)
    result = live.run_live("j1", "u", {"split_minutes": 1, "container": "mp4"})

    d = result["detail"]
    assert d["end_reason"] == "stream_ended"
    assert d["parts"] == 3 and d["container"] == "mp4"
    assert 125 <= d["duration"] <= 135
    names = sorted(Path(p).name for p, _, _ in fake_jobs.files)
    assert len(names) == 3 and all(n.startswith("100% Live Radio 2026-") for n in names)
    assert [n.rsplit(" ", 1)[-1] for n in names] == ["part001.mp4", "part002.mp4", "part003.mp4"]
    assert [(k, lbl) for _, k, lbl in fake_jobs.files] == [("part", "Part 1"), ("part", "Part 2"),
                                                           ("part", "Part 3")]
    assert not list(outdir.glob("*.ts"))              # every part was rewrapped
    ticks = [u for u in fake_jobs.updates if "rec_seconds" in u]
    assert ticks and max(u["rec_bytes"] for u in ticks) > 0     # size read from the parts on disk
    assert fake_jobs.fields["live_phase"] == "saving"


@needs_ffmpeg
def test_single_recording_limit_and_names(monkeypatch, fake_jobs, outdir, media_dir):
    t = _file_target(media_dir / "long.ts", "NASA Live 2026-09-23 18:51")
    monkeypatch.setattr(live, "resolve", lambda *a, **k: t)
    r1 = live.run_live("j1", "u", {"max_minutes": 0.25, "container": "mkv"})
    assert r1["detail"]["end_reason"] == "limit"
    assert r1["detail"]["container"] == "mkv" and r1["detail"]["parts"] == 1
    assert 14 <= r1["detail"]["duration"] <= 17
    r2 = live.run_live("j2", "u", {"max_minutes": 0.25, "container": "mkv"})
    first, second = (Path(p) for p, _, _ in fake_jobs.files)
    assert first != second and first.exists() and second.exists()   # same stream, same minute
    assert fake_jobs.files[0][1] == "video"
    assert fake_jobs.fields["rec_limit_seconds"] == 15


@needs_ffmpeg
@pytest.mark.parametrize("src, container, ext", [
    ("aac.ts", "m4a", ".m4a"), ("mp3.ts", "m4a", ".mp3"), ("opus.ts", "m4a", ".mka"),
    ("aac.ts", "ts", ".ts"), ("mp3.ts", "mp4", ".mp3"),
])
def test_sound_only_containers(monkeypatch, fake_jobs, outdir, media_dir, src, container, ext):
    t = _file_target(media_dir / src, "Radio", audio_only=True)
    monkeypatch.setattr(live, "resolve", lambda *a, **k: t)
    r = live.run_live("j1", "u", {"audio_only": True, "container": container})
    (path, kind, _), = fake_jobs.files
    assert Path(path).suffix == ext and kind == "audio"
    assert r["detail"]["container"] == ext[1:]
    assert sorted(p.suffix for p in outdir.iterdir()) == [ext]       # no leftovers, no 0-byte files


@needs_ffmpeg
def test_remux_never_leaves_an_empty_file(tmp_path, media_dir):
    bad = tmp_path / "broken.ts"
    bad.write_bytes(b"\x47" + b"\x00" * 5000)
    assert live.remux(bad, "mp4") == bad
    assert [p.name for p in tmp_path.iterdir()] == ["broken.ts"]
    src = tmp_path / "av.ts"
    src.write_bytes((media_dir / "av.ts").read_bytes())
    out = live.remux(src, "mp4")
    assert out.suffix == ".mp4" and out.stat().st_size > 0 and not src.exists()
    assert live.probe_streams(out) == [("Video", "h264"), ("Audio", "aac")]


# ------------------------------------------------ recording loop, no ffmpeg

class FakeFfmpeg:
    """Stands in for _run_ffmpeg: each call plays the next scripted session,
    writing the files the real command would have written."""

    def __init__(self, sessions, fake_jobs=None):
        self.sessions = list(sessions)
        self.fake_jobs = fake_jobs
        self.cmds = []

    def __call__(self, cmd, jid, on_tick, env=None, stalled=None):
        self.cmds.append(cmd)
        s = self.sessions.pop(0)
        sizes = s.get("sizes", [1000])
        if "-segment_start_number" in cmd:
            start = int(cmd[cmd.index("-segment_start_number") + 1])
            paths = [Path(cmd[-1] % n) for n in range(start, start + len(sizes))]
        else:
            paths = [Path(cmd[-1])][:len(sizes)]
        for p, size in zip(paths, sizes):
            p.write_bytes(b"\x47" * size)
        on_tick({"out_time_us": str(int(s.get("secs", 30) * 1e6)),
                 "total_size": str(sum(sizes)), "progress": "continue"})
        if s.get("stop") and self.fake_jobs:
            self.fake_jobs.stop.set()
        return s.get("code", 0), s.get("tail", ""), s.get("how", "exit")


@pytest.fixture
def no_media(monkeypatch):
    """Scripted sessions write filler bytes, not media: nothing to rewrap,
    and no waiting between retries."""
    monkeypatch.setattr(live, "probe", lambda path: ([], 0.0))
    monkeypatch.setattr(live, "_pause", lambda seconds, jid: not live.jobs.cancelled(jid))


def _live_target(**kw):
    t = _target(**kw)
    t.update(title="NASA Live 2026-09-23 18:51", meta={"title": "NASA Live"})
    return t


def test_a_dropped_session_continues_into_numbered_parts(monkeypatch, fake_jobs, outdir, no_media):
    """CRIT-1: a network drop used to end the recording for good as 'Done'.
    An empty session's file is deleted and its number used again."""
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    ff = FakeFfmpeg([
        {"sizes": [1000], "secs": 30, "tail": "[in#0/hls] Error during demuxing: Error number -138"},
        {"sizes": [0], "secs": 0, "tail": "Server returned 5XX Server Error reply"},
        {"sizes": [500], "secs": 20, "how": "user", "stop": True},
    ], fake_jobs)
    monkeypatch.setattr(live, "_run_ffmpeg", ff)
    r = live.run_live("j1", "https://www.youtube.com/watch?v=live", {})

    d = r["detail"]
    assert d["end_reason"] == "user" and d["parts"] == 2 and d["reconnects"] == 2
    assert d["duration"] == 50 and d["size"] == 1500
    names = [Path(p).name for p, _, _ in fake_jobs.files]
    assert [n.rsplit(" ", 1)[-1] for n in names] == ["part001.ts", "part002.ts"]
    assert all(n.startswith("NASA Live 2026-") for n in names)
    assert [(k, lbl) for _, k, lbl in fake_jobs.files] == [("part", "Part 1"), ("part", "Part 2")]
    assert sorted(p.name for p in outdir.iterdir()) == sorted(names)     # no empty leftovers
    assert "-n" in ff.cmds[1] and ff.cmds[1][-1].endswith("part002.ts")


def test_a_clean_end_of_a_finished_stream_is_stream_ended(monkeypatch, fake_jobs, outdir, no_media):
    calls = {"n": 0}

    def resolve(*a, **k):
        calls["n"] += 1
        t = _live_target()
        if calls["n"] > 1:                                  # after the session: it ended
            t.update(is_live=False, live_status="was_live")
        return t

    monkeypatch.setattr(live, "resolve", resolve)
    monkeypatch.setattr(live, "_run_ffmpeg", FakeFfmpeg([{"sizes": [2000], "secs": 42}]))
    r = live.run_live("j1", "u", {})
    assert r["detail"]["end_reason"] == "stream_ended" and r["detail"]["parts"] == 1
    (path, kind, label), = fake_jobs.files
    assert Path(path).name.endswith(".ts") and " part" not in Path(path).name
    assert kind == "audio" and label == ""        # filler bytes: no picture found in it


def test_connection_lost_keeps_what_was_recorded(monkeypatch, fake_jobs, outdir, no_media):
    calls = {"n": 0}

    def resolve(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _live_target()
        raise Exception("ERROR: Unable to download API page: [Errno 11001] getaddrinfo failed")

    monkeypatch.setattr(live, "resolve", resolve)
    monkeypatch.setattr(live, "RECONNECT_WINDOW", 0.0)
    monkeypatch.setattr(live, "_run_ffmpeg", FakeFfmpeg([
        {"sizes": [3000], "secs": 75, "tail": "Error during demuxing"}]))
    r = live.run_live("j1", "u", {})
    assert r["detail"]["end_reason"] == "connection_lost"
    assert r["detail"]["duration"] == 75 and len(fake_jobs.files) == 1


def test_limit_is_shared_across_sessions(monkeypatch, fake_jobs, outdir, no_media):
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    ff = FakeFfmpeg([{"sizes": [1000], "secs": 40, "tail": "error"},
                     {"sizes": [1000], "secs": 20}])
    monkeypatch.setattr(live, "_run_ffmpeg", ff)
    r = live.run_live("j1", "u", {"max_minutes": 1})
    assert r["detail"]["end_reason"] == "limit"
    assert ff.cmds[0][ff.cmds[0].index("-t") + 1] == "60"
    assert ff.cmds[1][ff.cmds[1].index("-t") + 1] == "20"    # only what is left
    assert fake_jobs.fields["rec_limit_seconds"] == 60


def test_split_sessions_keep_numbering(monkeypatch, fake_jobs, outdir, no_media):
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    ff = FakeFfmpeg([{"sizes": [1000, 1000, 0], "secs": 125, "tail": "error"},
                     {"sizes": [700], "secs": 30, "how": "user", "stop": True}], fake_jobs)
    monkeypatch.setattr(live, "_run_ffmpeg", ff)
    r = live.run_live("j1", "u", {"split_minutes": 1})
    assert ff.cmds[1][ff.cmds[1].index("-segment_start_number") + 1] == "3"
    names = sorted(Path(p).name.rsplit(" ", 1)[-1] for p, _, _ in fake_jobs.files)
    assert names == ["part001.ts", "part002.ts", "part003.ts"]
    assert r["detail"]["parts"] == 3 and r["detail"]["end_reason"] == "user"


@pytest.mark.parametrize("opts, sessions", [
    ({"split_minutes": 5, "max_minutes": 0.5}, [{"sizes": [900], "secs": 30}]),
    ({}, [{"sizes": [900], "secs": 30, "tail": "error"},
          {"sizes": [0], "secs": 0, "how": "user", "stop": True}]),
])
def test_a_lone_part_keeps_the_plain_name(monkeypatch, fake_jobs, outdir, no_media, opts, sessions):
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    monkeypatch.setattr(live, "_run_ffmpeg", FakeFfmpeg(sessions, fake_jobs))
    r = live.run_live("j1", "u", opts)
    (path, kind, label), = fake_jobs.files
    assert " part" not in Path(path).name and Path(path).exists()
    assert [p.name for p in outdir.iterdir()] == [Path(path).name]
    assert r["detail"]["parts"] == 1 and label == ""


def test_only_new_media_time_counts_as_a_sign_of_life(fake_jobs, outdir):
    """The card shows 'Reconnecting' when the job stops updating. ffmpeg's
    buffer flush as a dead session exits must not look like fresh data."""
    rec = live._Recorder("j1", "u", _live_target(), {}, outdir)
    try:
        rec.cur_out = outdir / "x.ts"
        rec._tick({"out_time_us": "10000000", "total_size": "1000"})
        n = len(fake_jobs.updates)
        assert fake_jobs.fields["rec_seconds"] == 10.0 and fake_jobs.fields["rec_bytes"] == 1000
        rec._tick({"out_time_us": "10000000", "total_size": "5000"})        # flush only
        rec._tick({"out_time_us": "10100000", "total_size": "5000"})        # jitter
        assert len(fake_jobs.updates) == n
        rec._tick({"out_time_us": "11000000", "total_size": "6000"})
        assert len(fake_jobs.updates) == n + 1 and fake_jobs.fields["rec_bytes"] == 6000

        clockless = live._Recorder("j1", "u", _live_target(), {}, outdir)
        clockless.cur_out = outdir / "y.ts"
        clockless._tick({"out_time_us": "N/A", "total_size": "300"})
        assert fake_jobs.fields["rec_bytes"] == 300                        # size is all it has
        live.release_stem(outdir, clockless.stem)
    finally:
        live.release_stem(outdir, rec.stem)


def test_hms_matches_the_rounded_duration():
    assert live._hms(39.96) == "0:40" and live._hms(3599.6) == "1:00:00" and live._hms(None) == "0:00"


def test_nothing_recorded_fails_with_the_real_cause(monkeypatch, fake_jobs, outdir, no_media):
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    ff = FakeFfmpeg([{"sizes": [0], "secs": 0, "tail": "HTTP error 403 Forbidden"}] * 3)
    monkeypatch.setattr(live, "_run_ffmpeg", ff)
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "u", {})
    assert e.value.code == "rate_limited" and len(ff.cmds) == 3
    assert not list(outdir.iterdir())


def test_a_full_drive_with_nothing_saved_is_not_a_cancel(monkeypatch, fake_jobs, outdir, no_media):
    """Nobody pressed Stop, so the card must say the drive is full, not
    'Cancelled' (the loop's default end reason is 'user')."""
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    ff = FakeFfmpeg([{"sizes": [0], "secs": 0, "code": 1,
                      "tail": "av_interleaved_write_frame(): No space left on device"}])
    monkeypatch.setattr(live, "_run_ffmpeg", ff)
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "u", {})
    assert e.value.code == "disk_full" and len(ff.cmds) == 1


def test_a_full_drive_keeps_what_was_recorded(monkeypatch, fake_jobs, outdir, no_media):
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    monkeypatch.setattr(live, "_run_ffmpeg", FakeFfmpeg([
        {"sizes": [5000], "secs": 40, "code": 1, "tail": "Error writing trailer: No space left on device"}]))
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "u", {"container": "mp4"})
    assert e.value.code == "disk_full"
    (path, kind, _), = fake_jobs.files
    assert Path(path).suffix == ".ts" and Path(path).exists()        # not rewrapped: no room


def test_a_header_only_file_is_not_a_recording(monkeypatch, fake_jobs, outdir, no_media):
    """Matroska writes its header before the first packet. A session that
    never got any media must not leave that behind as a 'recording'."""
    t = _live_target(vcodec="vp09.00.40.08")                  # recorded into .mkv
    monkeypatch.setattr(live, "resolve", lambda *a, **k: t)
    ff = FakeFfmpeg([{"sizes": [1200], "secs": 0, "tail": "HTTP error 403 Forbidden"}] * 3)
    monkeypatch.setattr(live, "_run_ffmpeg", ff)
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "u", {})
    assert e.value.code == "rate_limited" and ff.cmds[0][-1].endswith(".mkv")
    assert not list(outdir.iterdir()) and not fake_jobs.files


def test_a_socks_proxy_is_named_when_the_recording_fails(monkeypatch, fake_jobs, outdir, no_media):
    cfg = dict(live.config.get(), proxy="socks5://127.0.0.1:1080")
    monkeypatch.setattr(live.config, "get", lambda: dict(cfg))
    assert live._ffmpeg_env() is None                     # ffmpeg cannot use it at all
    monkeypatch.setattr(live, "resolve", lambda *a, **k: _live_target())
    monkeypatch.setattr(live, "_run_ffmpeg",
                        FakeFfmpeg([{"sizes": [0], "secs": 0, "tail": "HTTP error 403"}] * 3))
    with pytest.raises(errors.AppError) as e:
        live.run_live("j1", "u", {})
    assert e.value.code == "proxy" and "socks5" in e.value.detail


@pytest.mark.parametrize("proxy, expected", [
    ("", None), ("http://10.0.0.1:3128", "http://10.0.0.1:3128"),
    ("10.0.0.1:3128", "http://10.0.0.1:3128"), ("socks5://h:1", None), ("https://h:1", None),
])
def test_ffmpeg_gets_http_proxies_only(monkeypatch, proxy, expected):
    cfg = dict(live.config.get(), proxy=proxy)
    monkeypatch.setattr(live.config, "get", lambda: dict(cfg))
    env = live._ffmpeg_env()
    assert (env or {}).get("http_proxy") == expected
    if env:
        assert env["HTTP_PROXY"] == expected


def test_not_live_yet_reads_as_a_live_not_live_error():
    nl = live.NotLiveYet("The channel is not currently live", {"uploader": "Blender"})
    err = errors.classify(nl, "https://www.youtube.com/@BlenderOfficial/live", "live")
    assert err["code"] == "live_not_live" and err["title"] == "Blender isn't live right now"
    assert err["params"]["live_status"] == "offline"
    up = live.NotLiveYet("will begin in 1 hour", {"release_timestamp": 1_900_000_000}, True)
    err = errors.classify(up, "u", "live")
    assert err["params"] == {"release_timestamp": 1_900_000_000, "live_status": "is_upcoming"}
    assert "ERROR" not in err["title"] + err["body"]


def test_channel_name_is_looked_up_only_for_detailed_checks(monkeypatch):
    looked = []
    monkeypatch.setattr(live, "_channel_name", lambda url: looked.append(url) or "Blender")
    exc = Exception("ERROR: [youtube:tab] @BlenderOfficial: The channel is not currently live")
    url = "https://www.youtube.com/@BlenderOfficial/live"
    assert live._not_live_from(url, exc, False).meta["uploader"] == "BlenderOfficial"
    assert not looked
    assert live._not_live_from(url, exc, True).meta["uploader"] == "Blender"
    assert looked == [url]


def test_channel_name_ignores_links_it_cannot_use(monkeypatch):
    def boom(opts):
        raise AssertionError("no lookup expected")
    monkeypatch.setattr(live, "_ydl", boom)
    for url in ("https://www.twitch.tv/name", "https://www.youtube.com/watch?v=abc", "nonsense"):
        assert live._channel_name(url) == ""


def test_wait_keeps_the_better_channel_name(monkeypatch, fake_jobs):
    clock = Clock()
    monkeypatch.setattr(live.time, "time", clock.time)
    monkeypatch.setattr(live.time, "sleep", clock.sleep)

    def fake_resolve(url, quality="best", audio_only=False, details=True):
        name = "Blender" if details else "BlenderOfficial"
        raise live.NotLiveYet("The channel is not currently live", {"uploader": name})

    monkeypatch.setattr(live, "resolve", fake_resolve)
    with pytest.raises(errors.AppError) as e:
        live.wait_until_live("https://www.youtube.com/@BlenderOfficial/live", "j1",
                             deadline=clock.now + 3600)
    assert {u["uploader"] for u in fake_jobs.updates if "uploader" in u} == {"Blender"}
    assert errors.classify(e.value, "", "live")["title"] == "Blender isn't live right now"


def test_a_link_without_codec_facts_keeps_its_picture(monkeypatch, tmp_path):
    """A bare HLS link reports no codecs. Treating it as sound only recorded
    the audio of a video stream and threw the picture away."""
    info = {"title": "live", "url": "http://127.0.0.1/live.m3u8", "protocol": "m3u8_native",
            "ext": "mp4", "vcodec": None, "acodec": None}
    _fake_ydl(monkeypatch, info)
    t = live.resolve("http://127.0.0.1/live.m3u8")
    assert t["video"] and t["single"] and not t["audio"]
    cmd = live.build_command(t, tmp_path / "a.ts", {})
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"] == ["0:v:0?", "0:a:0?"]
    t = live.resolve("http://127.0.0.1/live.m3u8", "best", True)       # sound only
    assert t["audio"] and not t["video"]
    cmd = live.build_command(t, tmp_path / "a.ts", {})
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"] == ["0:a:0"]


@pytest.mark.parametrize("fmt, video, audio", [
    ({"vcodec": None, "acodec": None}, True, True),                   # unknown: may be both
    ({"vcodec": "none", "acodec": None}, False, True),                # YouTube audio rendition
    ({"vcodec": None, "acodec": "mp4a.40.2"}, False, True),           # radio
    ({"vcodec": "avc1", "acodec": "none"}, True, False),
    ({"height": 720}, True, False),
    ({"vcodec": "avc1", "acodec": "mp4a"}, True, True),
])
def test_track_guesses(fmt, video, audio):
    assert live._has_video(fmt) is video and live._has_audio(fmt) is audio


def test_ydl_never_writes_the_users_cookies(monkeypatch, tmp_path):
    """CRIT-2: yt-dlp rewrites its cookiefile on close. Each instance gets a
    throwaway copy, and the engine's own copy is handed back afterwards."""
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    seen, released = [], []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            seen.append(self.opts["cookiefile"])
            return self

        def __exit__(self, *exc):
            Path(self.opts["cookiefile"]).write_text("clobbered", encoding="utf-8")

    monkeypatch.setattr(live, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(live.media, "release", lambda opts: released.append(opts), raising=False)
    handed = {"cookiefile": str(jar), "quiet": True}
    with live._ydl(handed):
        pass
    assert seen and seen[0] != str(jar) and not Path(seen[0]).exists()
    assert jar.read_text(encoding="utf-8") == "# Netscape HTTP Cookie File\n"
    assert released == [handed]


def test_resolve_meta_is_built_here(monkeypatch):
    info = {"title": "Radio 2026-09-23 18:51", "is_live": True, "live_status": "is_live",
            "uploader": "Station", "url": "https://x/a.m3u8", "protocol": "m3u8_native",
            "acodec": "mp4a", "vcodec": "none", "webpage_url": "https://radio/x"}
    _fake_ydl(monkeypatch, info)
    monkeypatch.setattr(live.media, "summarize", lambda i: 1 / 0, raising=False)
    t = live.resolve("https://radio/x", "best", True)
    assert t["meta"]["title"] == "Radio" and t["meta"]["kind"] == "live"
    assert t["meta"]["url"] == "https://radio/x" and t["audio"] and not t["video"]


# ------------------------------------------------------------ _run_ffmpeg

def _lavfi_cmd(tmp_path, *extra, seconds=4):
    exe = live._ffmpeg()
    return [exe, "-hide_banner", "-nostats", "-loglevel", "warning", "-n", "-progress", "pipe:1",
            *extra, "-re", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", str(seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
            "-f", "segment", "-segment_time", "1", "-segment_format", "mpegts",
            str(tmp_path / "seg part%03d.ts")]


@needs_ffmpeg
def test_a_failing_tick_never_ends_the_session(tmp_path, fake_jobs):
    ticks = []

    def on_tick(stats):
        ticks.append(stats.get("total_size"))
        raise ValueError("boom")                     # the old code died here, on 'N/A'

    t0 = time.monotonic()
    code, tail, how = live._run_ffmpeg(_lavfi_cmd(tmp_path), "j1", on_tick)
    assert code == 0 and how == "exit"
    assert time.monotonic() - t0 >= 3.5              # ran to the end, not cut short
    assert len(ticks) >= 3 and "N/A" in ticks
    assert len(list(tmp_path.glob("seg part*.ts"))) >= 3


@needs_ffmpeg
def test_stop_works_without_any_progress_line(tmp_path, fake_jobs):
    """The timer thread sends 'q' even when no progress line reaches us (the
    old loop only looked at the cancel flag after reading one)."""
    cmd = _lavfi_cmd(tmp_path, seconds=120)
    cmd[cmd.index("pipe:1")] = str(tmp_path / "progress.txt")      # stdout stays silent
    threading.Timer(1.0, fake_jobs.stop.set).start()
    t0 = time.monotonic()
    code, tail, how = live._run_ffmpeg(cmd, "j1", lambda s: None)
    assert how == "user"
    assert time.monotonic() - t0 < 10


@needs_ffmpeg
def test_a_stalled_session_is_ended(tmp_path, fake_jobs):
    cmd = _lavfi_cmd(tmp_path, seconds=120)
    start = time.monotonic()
    code, tail, how = live._run_ffmpeg(cmd, "j1", lambda s: None,
                                       stalled=lambda: time.monotonic() - start > 1.5)
    assert how == "stalled"
    assert time.monotonic() - start < 10


# ----------------------------------------------------------------- network

@pytest.mark.network
def test_check_a_real_live_stream():
    r = live.check("https://www.youtube.com/@NASA/live")
    assert r["is_live"] or r["offline"]
