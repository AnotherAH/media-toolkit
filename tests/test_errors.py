"""Error catalog and classification (app/errors.py)."""
from __future__ import annotations

import errno

import pytest

from app import errors
from app.errors import AppError, classify, clean_detail, redact, site_name

YT = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


@pytest.mark.parametrize("text, code", [
    # the cases the audit found mislabelled or unhelpful
    ("The recording produced no file. ffmpeg said:\nServer returned 403 Forbidden", "rate_limited"),
    ("ERROR: ffmpeg exited with code 3436169992", "rate_limited"),        # AVERROR_HTTP_FORBIDDEN
    ("ERROR: ffmpeg exited with code 1", "convert_failed"),
    ("ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path "
     "using --ffmpeg-location", "ffmpeg_missing"),
    ("ERROR: You have requested merging of multiple formats but ffmpeg is not installed.",
     "ffmpeg_missing"),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser or "
     "--cookies for the authentication.", "rate_limited"),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for "
     "some users. Use --cookies-from-browser", "age"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "signin"),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", "signin"),
    ("ERROR: [Instagram] x: Instagram sent an empty media response.", "signin"),
    ("ERROR: [youtube] abc: Video unavailable. The uploader has not made this video available "
     "in your country", "geo"),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
     "unavailable"),
    ("ERROR: Unsupported URL: https://example.com/", "bad_link"),
    ("ERROR: 'hello' is not a valid URL.", "bad_link"),
    ("ERROR: Unable to download webpage: HTTP Error 404: Not Found", "bad_link"),
    ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>",
     "network"),
    ("ERROR: Unable to connect to proxy: [WinError 10061] No connection could be made", "proxy"),
    ("[WinError 10060] A connection attempt failed because the connected party did not respond",
     "network"),
    ("[Errno 28] No space left on device", "disk_full"),
    ("[WinError 112] There is not enough space on the disk", "disk_full"),
    ("[Errno 13] Permission denied: 'D:\\private\\404\\video.mp4'", "folder_denied"),
    ("ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271",
     "cookies_locked"),
    ("Failed to decrypt with DPAPI", "cookies_locked"),
    ("The channel is not currently live", "live_not_live"),
    ("This live event will begin in 3 hours.", "live_not_live"),
    ("Unable to open file 'model.bin' in model 'C:\\models\\x'", "model_damaged"),
    ("The base model did not download correctly (config.json missing)", "model_damaged"),
    ("CUDA failed with error out of memory", "out_of_memory"),
    ("Could not load library cudnn_ops64_9.dll", "gpu_failed"),
    ("No speech found in the audio", "no_speech"),
    ("x.mp4: Invalid data found when processing input", "unreadable_file"),
    ("ERROR: [generic] Unable to extract title; please report this issue on "
     "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template.",
     "site_changed"),
    ("something nobody has seen before", "unknown"),
])
def test_classify_codes(text, code):
    assert classify(RuntimeError(text), YT, "download")["code"] == code


def test_plain_ffmpeg_mention_is_not_missing_ffmpeg():
    """FIN-17 / DL-22: 'ffmpeg' alone must never say ffmpeg is missing."""
    for text in ("The recording produced no file. ffmpeg said: exit code 1",
                 "ffmpeg: something odd happened"):
        assert classify(RuntimeError(text), YT, "live")["code"] != "ffmpeg_missing"


def test_entry_shape_and_placeholders():
    err = classify(RuntimeError("HTTP Error 429: Too Many Requests"), YT, "download")
    assert set(err) >= {"code", "title", "body", "actions", "detail"}
    assert err["title"] == "YouTube is limiting downloads right now"
    assert "Signing in to YouTube" in err["body"]
    assert err["actions"] == ["retry", "signin"]
    for value in (err["title"], err["body"]):
        assert "{" not in value and "\u2014" not in value


def test_unknown_site_reads_naturally():
    err = errors.entry("rate_limited", "", "download")
    assert err["title"].startswith("This site is limiting")
    assert "Signing in to this site also helps." in err["body"]


def test_unknown_names_the_kind():
    assert classify(RuntimeError("??"), YT, "live")["title"] == "This recording didn't work"
    assert classify(RuntimeError("??"), YT, "transcript")["title"] == "This transcript didn't work"
    probe = classify(RuntimeError("??"), YT, "probe")
    assert probe["title"] == errors.PROBE_TITLE


def test_every_catalog_entry_is_complete():
    for code in errors.CATALOG:
        e = errors.entry(code, YT, "download")
        assert e["title"] and e["body"] and e["actions"]
        assert "{" not in e["title"] + e["body"], code
        assert "\u2014" not in e["title"] + e["body"], code


def test_app_error_params():
    exc = AppError("live_not_live", "ERROR: [youtube] x: The channel is not currently live",
                   channel="NASA", title="ISS live", thumbnail="https://i.ytimg.com/x.jpg")
    err = classify(exc, YT, "live")
    assert err["code"] == "live_not_live"
    assert err["title"] == "NASA isn't live right now"
    assert err["params"]["title"] == "ISS live"
    assert err["detail"] == "The channel is not currently live"
    assert AppError("no-such-code").code == "unknown"


def test_not_live_depends_on_where_it_happened():
    # Live tab, stream not started: offer to wait.
    live = classify(AppError("live_not_live", "offline", uploader="NASA"), YT, "live")
    assert live["actions"] == ["retry_wait"]
    # Live tab, a finished video: waiting would never end; offer the Download tab.
    regular = classify(AppError("live_not_live", "not live", live_status="not_live",
                                title="Big Buck Bunny"), YT, "live")
    assert regular["title"] == "This is a regular video, not a live stream"
    assert regular["actions"] == ["download_instead", "remove"]
    # Download tab, a premiere that has not started: the Live tab can wait for it.
    dl = classify(RuntimeError("ERROR: [youtube] x: This live event will begin in 3 hours."),
                  YT, "download")
    assert dl["code"] == "live_not_live" and dl["actions"] == ["record_live", "remove"]
    assert "Live tab" in dl["body"]


def test_detail_is_cleaned_but_complete():
    text = ("ERROR: [youtube] aqz-KE-bpKQ: Unable to extract something; please report this issue "
            "on https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue "
            "template. Confirm you are on the latest version using yt-dlp -U")
    assert clean_detail(text) == "Unable to extract something"
    long = "x" * 5000
    assert len(classify(RuntimeError(long))["detail"]) >= 5000
    # a prefix that is not an extractor tag keeps its words
    assert clean_detail("ERROR: Postprocessing: Conversion failed!") == "Postprocessing: Conversion failed!"


def test_exception_types():
    assert classify(TimeoutError("read"), YT)["code"] == "network"
    assert classify(PermissionError("nope"), YT)["code"] == "folder_denied"
    assert classify(OSError(errno.ENOSPC, "full"), YT)["code"] == "disk_full"


def test_folder_placeholder_uses_the_config_folder():
    err = classify(PermissionError(13, "Permission denied"), YT, "download")
    assert "Choose a different folder" in err["body"]
    assert "{folder}" not in err["body"]


def test_ffmpeg_exit_decoding():
    assert errors.ffmpeg_exit_reason(3436169992)[0] == "rate_limited"
    assert errors.ffmpeg_exit_reason(1) is None
    assert errors.ffmpeg_exit_reason((1 << 32) - 28)[0] == "disk_full"


@pytest.mark.parametrize("url, name", [
    ("https://youtu.be/x", "YouTube"), ("https://m.youtube.com/watch?v=1", "YouTube"),
    ("https://www.instagram.com/p/x", "Instagram"), ("https://twitter.com/a/status/1", "X"),
    ("https://x.com/a", "X"), ("https://fb.watch/x", "Facebook"), ("https://www.twitch.tv/x", "Twitch"),
    ("https://vimeo.com/1", "Vimeo"), ("https://old.reddit.com/r/x", "Reddit"),
    ("https://kick.com/x", "Kick"), ("https://www.example.org/v", "example.org"),
    ("not a url", ""), ("", ""),
])
def test_site_name(url, name):
    assert site_name(url) == name


def test_redact():
    text = ("Cookie: SID=abc; HSID=def\nfetching https://rr1---sn-x.googlevideo.com/videoplayback"
            "?expire=1&sig=SECRET&ip=1.2.3.4 and https://www.youtube.com/watch?v=abc&token=XYZ "
            "via http://user:pass@proxy.local:8080")
    out = redact(text)
    for secret in ("abc; HSID", "SECRET", "1.2.3.4", "XYZ", "user:pass"):
        assert secret not in out
    assert "watch?v=abc" in out
    assert "googlevideo.com/[hidden]" in out
    assert "X-MT-Token: [hidden]" in redact("X-MT-Token: 123")
