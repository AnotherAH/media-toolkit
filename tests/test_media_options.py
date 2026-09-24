"""Download option mapping (app/media.py): what each choice becomes in yt-dlp.

Offline: nothing here reaches a site. The options are checked as data, and a
few are handed to a real YoutubeDL to prove yt-dlp accepts their shape.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app import config, media


@pytest.fixture(autouse=True)
def clean_config(tmp_path):
    """Defaults for every test, with folders inside the test's own tmp dir."""
    keep = config.get()
    config.save({**config.DEFAULTS, "download_dir": str(tmp_path / "dl"),
                 "transcript_dir": str(tmp_path / "tr"), "setup_complete": True})
    yield
    config.save(keep)


def build(opts=None, outdir="C:/dl", temp=None):
    return media.build_download_opts(dict(opts or {}), outdir, temp)


def keys(o):
    return [p["key"] for p in o["postprocessors"]]


def pp(o, key):
    return next(p for p in o["postprocessors"] if p["key"] == key)


# ------------------------------------------------------------------ rates

@pytest.mark.parametrize("text,bare,expected", [
    ("5M", "M", 5 * 1024 ** 2),
    ("5 MB/s", "M", 5 * 1024 ** 2),
    ("5MB", "M", 5 * 1024 ** 2),
    ("1.5 MiB/s", "M", int(1.5 * 1024 ** 2)),
    ("500K", "M", 500 * 1024),
    ("500 kb/s", "M", 500 * 1024),
    ("5", "M", 5 * 1024 ** 2),            # the field says MB/s
    ("100", "K", 100 * 1024),
    ("2,5 M", "M", int(2.5 * 1024 ** 2)),
    ("1G", "M", 1024 ** 3),
    ("", "M", None),
    ("fast", "M", None),
    ("5 MB per second", "M", None),
    ("0", "M", None),
])
def test_parse_rate(text, bare, expected):
    assert media._parse_rate(text, bare=bare) == expected


def test_rate_limit_setting_reaches_yt_dlp():
    config.save({"rate_limit": "5 MB/s", "throttled_rate": "100"})
    o = media.base_opts()
    assert o["ratelimit"] == 5 * 1024 ** 2
    assert o["throttledratelimit"] == 100 * 1024
    media.release(o)


# ------------------------------------------------------------- base opts

def test_base_opts_cookie_file_is_a_private_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_COOKIE_DIR", tmp_path / "copies")
    jar = tmp_path / "cookies.txt"
    text = "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n"
    jar.write_text(text, encoding="utf-8")
    config.save({"cookies_file": str(jar)})
    a, b = media.base_opts(), media.base_opts()
    assert a["cookiefile"] != b["cookiefile"] != str(jar)
    assert Path(a["cookiefile"]).parent == tmp_path / "copies"
    assert Path(a["cookiefile"]).read_text(encoding="utf-8") == text
    # yt-dlp rewrites its cookie file on close; the user's file must not change.
    with media.session(a) as ydl:
        ydl.cookiejar.set_cookie(__import__("http.cookiejar").cookiejar.Cookie(
            0, "added", "x", None, False, ".example.com", True, True, "/", True,
            False, 2 ** 31, False, None, None, {}))
    assert jar.read_text(encoding="utf-8") == text
    assert not os.path.exists(a["cookiefile"])          # session() released it
    media.release(b)
    assert not os.path.exists(b["cookiefile"])


def test_base_opts_missing_cookie_file_is_ignored(tmp_path):
    config.save({"cookies_file": str(tmp_path / "gone.txt"), "cookies_browser": ""})
    assert "cookiefile" not in media.base_opts()


def test_browser_cookies(tmp_path):
    config.save({"cookies_browser": "firefox", "cookies_profile": "work"})
    o = media.base_opts()
    assert o["cookiesfrombrowser"] == ("firefox", "work", None, None)
    assert "cookiefile" not in o


def test_external_downloader_only_aria2c(monkeypatch):
    config.save({"external_downloader": r"C:\evil\pwn.bat"})
    assert "external_downloader" not in media.base_opts()
    config.save({"external_downloader": "aria2c"})
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    assert "external_downloader" not in media.base_opts()
    monkeypatch.setattr(media.shutil, "which", lambda name: r"C:\bin\aria2c.exe")
    assert media.base_opts()["external_downloader"] == {"default": "aria2c"}


def test_js_runtimes_are_enabled(monkeypatch, tmp_path):
    node = tmp_path / "node.exe"
    node.write_bytes(b"")
    monkeypatch.setattr(media, "_js_cache", (0.0, {}))
    monkeypatch.setattr(media.shutil, "which",
                        lambda name: str(node) if name == "node" else None)
    monkeypatch.setattr(config, "BIN_DIR", tmp_path / "nobin")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "noruntime")
    assert media.js_runtimes() == {"node": {"path": str(node)}}
    assert media.base_opts()["js_runtimes"] == {"node": {"path": str(node)}}
    assert media.js_runtime_name() == "node"


def test_js_runtime_qjs_maps_to_quickjs(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "_js_cache", (0.0, {}))
    monkeypatch.setattr(media.shutil, "which", lambda name: "/q/qjs" if name == "qjs" else None)
    monkeypatch.setattr(config, "BIN_DIR", tmp_path / "nobin")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "noruntime")
    assert media.js_runtimes() == {"quickjs": {"path": "/q/qjs"}}


def test_js_runtime_shape_is_accepted_by_yt_dlp(monkeypatch):
    from yt_dlp import YoutubeDL
    monkeypatch.setattr(media, "js_runtimes", lambda: {"node": {"path": "node"}})
    with YoutubeDL({"quiet": True, "js_runtimes": media.js_runtimes()}) as ydl:
        assert "node" in ydl.params["js_runtimes"]


def test_impersonate_family_only_when_available(monkeypatch):
    from yt_dlp.networking.impersonate import ImpersonateTarget
    monkeypatch.setattr(media, "_imp_cache", [ImpersonateTarget("chrome", "131", "windows", "10"),
                                             ImpersonateTarget("safari", "18", "macos", "15")])
    assert media.impersonate_targets() == ["chrome", "safari"]
    config.save({"impersonate": "chrome"})
    assert str(media.base_opts()["impersonate"]) == "chrome"
    config.save({"impersonate": "Chrome-131"})
    assert media.base_opts()["impersonate"] is not None
    config.save({"impersonate": "firefox"})
    assert "impersonate" not in media.base_opts()       # yt-dlp would refuse to start
    monkeypatch.setattr(media, "_imp_cache", [])
    config.save({"impersonate": "chrome"})
    assert "impersonate" not in media.base_opts()


def test_geo_country_is_validated():
    config.save({"geo_bypass_country": "us"})
    assert media.base_opts()["geo_bypass_country"] == "US"
    config.save({"geo_bypass_country": "USA!"})
    assert "geo_bypass_country" not in media.base_opts()


# --------------------------------------------------------- folders, names

def test_request_cannot_choose_the_folder_or_name_pattern():
    o = build({"output_dir": r"C:\Windows\Temp\x",
               "output_template": "../../%(title)s.%(ext)s"}, outdir="C:/dl")
    assert o["paths"]["home"] == "C:/dl"
    assert all(".." not in t for t in o["outtmpl"].values())
    assert "Windows" not in str(o["outtmpl"]) + str(o["paths"])


def test_paths_are_always_set_and_templates_relative():
    o = build(outdir="C:/dl", temp="C:/t/dl-1")
    assert o["paths"] == {"home": "C:/dl", "temp": "C:/t/dl-1"}
    for kind, tmpl in o["outtmpl"].items():
        assert not re.match(r"^[A-Za-z]:|^[\\/]", tmpl), kind
    assert o["outtmpl"]["chapter"]            # split chapters land in home, not the CWD
    assert o["outtmpl"]["pl_thumbnail"] == ""


def test_staging_off_still_sets_home():
    config.save({"use_temp_dir": False})
    o = build(outdir="C:/dl", temp="C:/t/dl-1")
    assert o["paths"] == {"home": "C:/dl"}


def test_staging_is_not_disabled_by_cover_art():
    o = build({"embed_thumbnail": True, "convert_thumbnails": "jpg"}, temp="C:/t/x")
    assert o["paths"].get("temp") == "C:/t/x"


def test_bad_configured_template_falls_back():
    config.save({"output_template": "C:/Windows/%(title)s.%(ext)s"})
    o = build()
    assert o["outtmpl"]["default"].startswith("%(title)")


def test_title_is_shortened_not_the_id():
    t = media._fit("%(title).180B [%(id)s].%(ext)s", 100)
    assert t == "%(title).100s [%(id)s].%(ext)s"
    assert media._fit("%(title)s.%(ext)s", 90) == "%(title).90s.%(ext)s"
    assert media._fit("%(uploader)s - %(title).50B.%(ext)s", 90) == "%(uploader)s - %(title).50s.%(ext)s"


def test_deep_folder_leaves_room_for_the_id():
    from yt_dlp import YoutubeDL
    home = "C:/" + "deep/" * 30                         # 153 characters
    names = media.file_templates(config.get(), home, None)
    title = "A" * 300
    with YoutubeDL({"quiet": True, "outtmpl": names["single"], "paths": {"home": home},
                    "trim_file_name": names["trim"]}) as ydl:
        path = ydl.prepare_filename({"id": "abcdefghijk", "title": title, "ext": "mp4"})
    assert path.endswith("[abcdefghijk].mp4")
    assert len(path) <= media.PATH_LIMIT


def test_suffix_marker_goes_before_the_extension():
    assert media._with_suffix("%(title)s [%(id)s].%(ext)s") == "%(title)s [%(id)s]%(mt_suffix|)s.%(ext)s"
    assert media._with_suffix("%(title)s") == "%(title)s%(mt_suffix|)s"


def test_playlist_templates_use_a_folder_and_index():
    o = build({"playlist_mode": "all"})
    pl = o["_playlist"]["outtmpl"]
    assert pl["default"].startswith("%(playlist_title,playlist_id|Playlist)")
    assert "/%(playlist_index)03d - " in pl["default"]
    assert pl["pl_thumbnail"] == ""
    assert o["_playlist"]["ignoreerrors"] == "only_download"


def test_playlist_thumbnail_only_when_asked():
    o = build({"playlist_mode": "all", "write_thumbnail": True})
    assert o["_playlist"]["outtmpl"]["pl_thumbnail"]
    assert o["allow_playlist_files"] is True
    assert build({"playlist_mode": "all"})["allow_playlist_files"] is False


def test_job_temp_dir_is_stable_per_request():
    a = media.job_temp_dir("https://x/1", {"quality": "720"})
    assert a == media.job_temp_dir("https://x/1", {"quality": "720"})
    assert a != media.job_temp_dir("https://x/1", {"quality": "1080"})
    assert a != media.job_temp_dir("https://x/2", {"quality": "720"})
    assert os.path.basename(a).startswith("dl-")


# ----------------------------------------------------------------- formats

def test_capped_presets_use_res_so_vertical_video_keeps_its_resolution():
    for q in ("2160", "1440", "1080", "720", "480"):
        fmt, sort = media.format_choice(q, False, "mkv")
        assert fmt == "bv*+ba/b"
        assert sort == [f"res:{q}"]
        assert "height<=" not in fmt


def test_compatible_prefers_h264_with_cap_before_quality():
    fmt, sort = media.format_choice("1080", True, "mp4")
    assert sort[0] == "vcodec:h264"
    assert sort.index("res:1080") < sort.index("quality")
    assert sort[-1] == "acodec:aac"      # last, or a 360p file with sound would win


def test_smallest_never_uses_worst():
    fmt, sort = media.format_choice("smallest", False, "mkv")
    assert "w" not in fmt.replace("bv*+ba/b", "")
    assert sort[0] == "lang" and "+size" in sort


def test_legacy_compatible_quality():
    assert media.format_choice("compatible", False, "mp4") == media.format_choice("best", True, "mp4")
    o = build({"quality": "compatible", "compatible": False})
    assert o["format_sort"][0] == "vcodec:h264"


def test_mp4_without_compatible_prefers_aac_sound_last():
    fmt, sort = media.format_choice("best", False, "mp4")
    assert sort[-1] == "acodec:aac" and "vcodec:h264" not in sort


def test_default_is_1080_h264_mp4():
    o = build()
    assert o["format_sort"][:3] == ["vcodec:h264", "lang", "res:1080"]
    assert o["merge_output_format"] == "mp4"


def test_format_sort_is_accepted_by_yt_dlp():
    from yt_dlp import YoutubeDL
    for q in ("best", "1080", "smallest"):
        for c in (True, False):
            _, sort = media.format_choice(q, c, "mp4")
            with YoutubeDL({"quiet": True, "format_sort": sort, "format": "bv*+ba/b"}) as ydl:
                ydl.build_format_selector("bv*+ba/b")


def test_webm_falls_back_to_mkv_and_is_not_remuxed():
    o = build({"container": "webm"})
    assert o["merge_output_format"] == "webm/mkv"
    assert "FFmpegVideoRemuxer" not in keys(o)


def test_remux_keeps_audio_only_sources():
    rule = pp(build({"container": "mkv"}), "FFmpegVideoRemuxer")["preferedformat"]
    assert rule.endswith("/mkv") and "mp3>mp3" in rule and "m4a>m4a" in rule


def test_exact_format_pick():
    o = build({"format_id": "137"})
    assert o["format"] == "137+ba/137"
    assert "format_sort" not in o
    assert "FFmpegVideoRemuxer" not in keys(o)
    o = build({"format_id": "137; rm -rf"})
    assert o["format"] == "bv*+ba/b"
    o = build({"mode": "audio", "format_id": "251-drc"})
    assert o["format"] == "251-drc"


# --------------------------------------------------------------- audio mode

def test_audio_mode_extracts_and_prefers_matching_source():
    o = build({"mode": "audio", "audio_codec": "m4a"})
    assert o["format"] == "ba/b"
    assert o["format_sort"] == ["lang", "acodec:aac"]
    assert pp(o, "FFmpegExtractAudio")["preferredcodec"] == "m4a"
    assert o["final_ext"] == "m4a"


def test_audio_normalize_uses_the_encoding_extractor():
    o = build({"mode": "audio", "audio_codec": "mp3", "normalize_audio": True})
    assert "FFmpegExtractAudio" not in keys(o)
    assert pp(o, "MTExtractAudio")["preferredcodec"] == "mp3"


def test_wav_skips_cover_art_entirely():
    o = build({"mode": "audio", "audio_codec": "wav", "embed_thumbnail": True})
    assert "MTEmbedThumbnail" not in keys(o)
    assert not o.get("writethumbnail")


def test_wav_keeps_a_thumbnail_file_when_asked():
    o = build({"mode": "audio", "audio_codec": "wav", "write_thumbnail": True})
    assert o["writethumbnail"] is True


def test_aac_is_treated_as_m4a():
    o = build({"mode": "audio", "audio_codec": "aac"})
    assert pp(o, "FFmpegExtractAudio")["preferredcodec"] == "m4a"


def test_audio_mode_ignores_subtitles():
    o = build({"mode": "audio", "subtitles": "all"})
    assert not o.get("writesubtitles")


# ------------------------------------------------------------ video extras

def test_normalize_without_encoder_levels_the_sound_only():
    o = build({"normalize_audio": True, "recode_encoder": ""})
    assert "MTNormalizeAudio" in keys(o)
    assert "MTForceRecode" not in keys(o)


def test_recode_carries_container_and_normalize():
    o = build({"recode_encoder": "hevc_nvenc", "container": "mkv", "normalize_audio": True})
    spec = pp(o, "MTForceRecode")
    assert spec == {"key": "MTForceRecode", "encoder": "hevc_nvenc", "quality": "balanced",
                    "container": "mkv", "normalize": True}
    assert o["final_ext"] == "mkv"
    assert "MTNormalizeAudio" not in keys(o)       # done in the same pass
    assert "FFmpegVideoRemuxer" not in keys(o)


def test_unknown_encoder_is_ignored():
    assert "MTForceRecode" not in keys(build({"recode_encoder": "evil; del"}))


def test_chapters_without_metadata():
    o = build({"embed_metadata": False, "embed_chapters": True, "embed_thumbnail": False})
    meta = pp(o, "FFmpegMetadata")
    assert meta["add_metadata"] is False and meta["add_chapters"] is True


def test_no_metadata_step_when_neither_is_wanted():
    o = build({"embed_metadata": False, "embed_chapters": False})
    assert "FFmpegMetadata" not in keys(o)


def test_metadata_runs_before_cover_art():
    ks = keys(build())
    assert ks.index("FFmpegMetadata") < ks.index("MTEmbedThumbnail")


def test_thumbnail_conversion_before_download():
    spec = pp(build({"convert_thumbnails": "jpg"}), "FFmpegThumbnailsConvertor")
    assert spec["when"] == "before_dl" and spec["format"] == "jpg"


def test_thumbnail_format_alone_writes_no_image():
    o = build({"convert_thumbnails": "png", "embed_thumbnail": False, "write_thumbnail": False})
    assert not o.get("writethumbnail")


def test_extras():
    o = build({"write_description": True, "write_info_json": True, "write_link": True,
               "write_comments": True, "max_comments": 25, "write_thumbnail": True})
    assert o["writedescription"] and o["writeinfojson"] and o["writethumbnail"]
    assert o["getcomments"] is True and o["_comments"] == 25
    assert o["extractor_args"]["youtube"]["max_comments"] == ["25", "25", "0", "0"]
    assert o["writeurllink"] is (os.name == "nt")
    assert pp(o, "MTEmbedThumbnail")["already_have_thumbnail"] is True


def test_comments_alone_do_not_force_the_info_json():
    assert not build({"write_comments": True}).get("writeinfojson")


# ----------------------------------------------------------- sponsorblock

def test_sponsorblock_remove_and_chapter_removal_share_one_step():
    o = build({"sponsorblock": True, "remove_chapters": "intro, credits"})
    assert keys(o).count("ModifyChapters") == 1
    mc = pp(o, "ModifyChapters")
    assert set(mc["remove_sponsor_segments"]) == set(media.DEFAULT_SPONSOR)
    assert len(mc["remove_chapters_patterns"]) == 2
    assert pp(o, "SponsorBlock")["when"] == "after_filter"


def test_sponsorblock_mark_mode_marks_chapters():
    o = build({"sponsorblock": True, "sponsorblock_mode": "mark",
               "sponsorblock_categories": ["sponsor", "outro", "bogus"],
               "embed_metadata": False, "embed_chapters": False})
    assert pp(o, "SponsorBlock")["categories"] == ["sponsor", "outro"]
    assert pp(o, "ModifyChapters")["remove_sponsor_segments"] == []
    assert pp(o, "FFmpegMetadata")["add_chapters"] is True


def test_sponsorblock_categories_from_text():
    o = build({"sponsorblock": True, "sponsorblock_categories": "selfpromo, intro"})
    assert pp(o, "SponsorBlock")["categories"] == ["selfpromo", "intro"]


def test_sponsor_categories_are_known_to_yt_dlp():
    from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP
    assert set(media.SPONSOR_CATEGORIES) <= set(SponsorBlockPP.CATEGORIES)


@pytest.mark.parametrize("pattern,title,hit", [
    ("intro", "Intro", True),
    ("intro", "The intro music", True),
    ("intro", "Introduction", False),
    ("C++", "C++ basics", True),
    ("C++", "Circadian Rhythm", False),
    ("Q&A (live", "Q&A (live) part", True),
    ("credits", "End Credits", True),
    ("مقدمه", "مقدمه فیلم", True),
])
def test_chapter_names_are_plain_text(pattern, title, hit):
    assert bool(media._chapter_pattern(pattern).search(title)) is hit


# -------------------------------------------------------------- subtitles

def test_subtitle_modes():
    assert media.subtitle_request({"subtitles": "none"}, config.get()) is None
    langs, auto = media.subtitle_request({"subtitles": "en"}, config.get())
    assert langs == ["en(?:-.*)?"] and auto is False
    langs, auto = media.subtitle_request({"subtitles": "all", "auto_subs": True}, config.get())
    assert langs == ["all", "-live_chat"] and auto is False
    langs, auto = media.subtitle_request({"subtitles": "custom", "subtitle_langs": "fa, es;x",
                                          "auto_subs": True}, config.get())
    assert langs == ["fa(?:-.*)?"] and auto is True


def test_subtitle_legacy_values():
    langs, auto = media.subtitle_request({"subtitles": "both", "subtitle_langs": "de"}, config.get())
    assert langs == ["de(?:-.*)?"] and auto is True
    langs, auto = media.subtitle_request({"subtitles": "manual", "subtitle_langs": "pt-BR"}, config.get())
    assert langs == [r"pt\-BR(?:-.*)?"] and auto is False


def test_subtitle_patterns_match_yt_dlp_keys():
    langs, _ = media.subtitle_request({"subtitles": "custom", "subtitle_langs": "en"}, config.get())
    rx = re.compile(langs[0] + "$")
    assert rx.match("en") and rx.match("en-US") and rx.match("en-orig")
    assert not rx.match("eng") and not rx.match("fr")


def test_subtitles_embedded_or_converted():
    o = build({"subtitles": "en", "embed_subs": True})
    assert o["writesubtitles"] and pp(o, "FFmpegEmbedSubtitle")
    o = build({"subtitles": "en", "embed_subs": False})
    assert pp(o, "FFmpegSubtitlesConvertor")["format"] == "srt"
    assert build({"subtitles": "en", "container": "webm"})["subtitlesformat"] == "vtt/best"


def test_remembered_download_choices_are_used():
    config.save({"dl_mode": "audio", "dl_audio_codec": "flac"})
    o = build({})
    assert pp(o, "FFmpegExtractAudio")["preferredcodec"] == "flac"
    o = build({"mode": "video"})
    assert "FFmpegExtractAudio" not in keys(o)


# ----------------------------------------------------------------- playlists

def test_playlist_modes():
    assert build({"playlist_mode": "video"})["noplaylist"] is True
    assert build({})["noplaylist"] is True
    assert build({"playlist": True})["noplaylist"] is False
    assert build({"playlist_mode": "all"})["noplaylist"] is False
    o = build({"playlist_mode": "first", "playlist_first": 3})
    assert o["playlist_items"] == "1:3" and o["noplaylist"] is False
    assert build({"playlist_mode": "first"})["playlist_items"] == "1:10"
    assert build({"playlist_mode": "items", "playlist_items": "1-5, 8"})["playlist_items"] == "1-5,8"
    with pytest.raises(ValueError):
        build({"playlist_mode": "items", "playlist_items": "1-5; rm"})


def test_playlist_options_apply_whenever_the_link_is_a_playlist():
    o = build({"archive": True, "stop_at_known": True, "playlist_order": "reverse",
               "max_downloads": 3}, outdir="C:/dl")
    assert o["download_archive"].replace("\\", "/") == "C:/dl/.download-archive.txt"
    assert o["break_on_existing"] is True
    assert o["playlistreverse"] is True
    assert o["max_downloads"] == 3
    assert build({"playlist_order": "random"})["playlistrandom"] is True


def test_concat_adds_the_playlist_step_and_one_codec():
    o = build({"playlist_mode": "all", "concat_playlist": True, "compatible": False})
    concat = pp(o, "FFmpegConcat")
    assert concat == {"key": "FFmpegConcat", "only_multi_video": False, "when": "playlist"}
    assert o["format_sort"][0] == "vcodec:h264"
    assert "MTEmbedThumbnail" not in keys(o)
    assert "concat_playlist" not in o


def test_postprocessor_shapes_are_accepted_by_yt_dlp():
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import get_postprocessor
    o = build({"sponsorblock": True, "remove_chapters": "x", "split_chapters": True,
               "subtitles": "en", "convert_thumbnails": "png", "concat_playlist": True})
    with YoutubeDL({"quiet": True}) as ydl:
        for spec in o["postprocessors"]:
            spec = dict(spec)
            key = spec.pop("key")
            spec.pop("when", None)
            if key.startswith("MT"):
                continue
            get_postprocessor(key)(ydl, **spec)


# ------------------------------------------------------------------ sections

@pytest.mark.parametrize("spec,ranges,chapters", [
    ("1:30-4:15", [(90.0, 255.0)], []),
    ("0:05-0:08, 1:00-1:10", [(5.0, 8.0), (60.0, 70.0)], []),
    ("*10:00-inf", [(600.0, float("inf"))], []),
    ("10:00-", [(600.0, float("inf"))], []),
    ("0:05–0:08", [(5.0, 8.0)], []),
    ("1:30", [(90.0, float("inf"))], []),
    ("intro, Q&A (live", [], ["intro", "Q&A (live"]),
    ("", [], []),
])
def test_sections(spec, ranges, chapters):
    assert media.parse_sections(spec) == (ranges, chapters)


def test_sections_become_download_ranges_and_record_misses():
    o = build({"section": "0:05-0:08, Credits"})
    ranges = o["download_ranges"]
    info = {"id": "x", "duration": 60, "chapters": [{"title": "Intro", "start_time": 0, "end_time": 5}]}

    class Y:
        def to_screen(self, *_):
            pass

    got = list(ranges(info, Y()))
    assert {"start_time": 5.0, "end_time": 8.0} in got
    assert o["_missed_chapters"] == ["Credits"]
    assert o["force_keyframes_at_cuts"] is True


def test_chapter_sections_match_case_insensitively():
    o = build({"section": "intro"})
    info = {"id": "x", "duration": 60, "chapters": [{"title": "Intro", "start_time": 0, "end_time": 5}]}

    class Y:
        def to_screen(self, *_):
            pass

    assert [c["title"] for c in o["download_ranges"](info, Y())] == ["Intro"]
    assert o["_missed_chapters"] == []


# ------------------------------------------------------------------ filters

def test_filters_plain_reasons():
    f = media.Filters({"min_duration": 1, "max_duration": 3, "min_views": 1000,
                       "title_contains": "Tutorial", "date_after": "2024-01-01",
                       "date_before": "2024/12/31", "max_filesize_mb": 500})
    assert f.check({"is_live": True}) == "a live stream"
    assert f.check({"live_status": "is_upcoming"}) == "a live stream that hasn't started yet"
    assert f.check({"duration": 30}) == "shorter than your 1-minute minimum"
    assert media.Filters({"min_duration": 2}).check({"duration": 30}) == \
        "shorter than your 2-minute minimum"
    f2 = media.Filters({"max_duration": 1})
    assert f2.check({"duration": 120}) == "longer than your 1-minute limit"
    assert media.Filters({"max_duration": 1.5}).check({"duration": 120}) == \
        "longer than your 1.5-minute limit"
    assert f.check({"duration": 100, "view_count": 10}) == "fewer than 1,000 views"
    assert f.check({"duration": 100, "title": "Cooking"}) == "the title doesn't include “Tutorial”"
    assert f.check({"duration": 100, "title": "a TUTORIAL", "upload_date": "20231231"}) == \
        "uploaded before 1 Jan 2024"
    assert f.check({"duration": 100, "title": "tutorial", "upload_date": "20250101"}) == \
        "uploaded after 31 Dec 2024"


def test_filters_only_judge_known_fields():
    f = media.Filters({"min_views": 1000, "title_contains": "x", "date_after": "20240101"})
    assert f.check({"title": "has x"}, incomplete=True) is None
    assert f.check({}, incomplete=True) is None


def test_size_is_checked_on_the_chosen_format_only():
    f = media.Filters({"max_filesize_mb": 100})
    big = {"requested_formats": [{"filesize": 80 * 2 ** 20}, {"filesize_approx": 30 * 2 ** 20}]}
    assert f.check(big, incomplete={"filesize"}) is None
    assert f.check(big, incomplete=False) == "larger than your 100 MB limit"
    assert f.check({"filesize_approx": 10 * 2 ** 20}, incomplete=False) is None
    assert media.Filters({"max_filesize": "500M"}).max_bytes == 500 * 2 ** 20


def test_title_filter_is_plain_text_not_a_regex():
    f = media.Filters({"title_contains": "C++"})
    assert f.check({"title": "Learn C++ today"}) is None
    assert f.check({"title": "ScienceCasts: The Power of Light"}) is not None
    f = media.Filters({"title_contains": "(draft"})
    assert f.check({"title": "Notes (draft 2)"}) is None


def test_max_filesize_also_guards_plain_downloads():
    o = build({"max_filesize_mb": 50})
    assert o["max_filesize"] == 50 * 2 ** 20


@pytest.mark.parametrize("text,expected", [
    ("2024-01-01", "20240101"),
    ("2024/01/31", "20240131"),
    ("20240101", "20240101"),
    ("", ""),
])
def test_normalize_date(text, expected):
    assert media.normalize_date(text) == expected


def test_relative_dates_and_bad_dates():
    assert re.fullmatch(r"\d{8}", media.normalize_date("today-1week"))
    with pytest.raises(ValueError, match="isn't a date"):
        media.normalize_date("01/2024")
    with pytest.raises(ValueError):
        media.normalize_date("2024-13-45")


# ---------------------------------------------------------------- misc

def test_pp_stages_never_show_class_names():
    assert media.pp_stage("Merger") == "Joining video and audio…"
    assert media.pp_stage("ExtractAudio", codec="mp3") == "Converting to MP3…"
    assert media.pp_stage("VideoRemuxer", ext="mp4") == "Converting to MP4…"
    assert media.pp_stage("Metadata") == "Adding title and artist…"
    assert media.pp_stage("Metadata", metadata=False) == "Adding chapter markers…"
    assert media.pp_stage("ModifyChapters") == "Cutting sponsor segments…"
    assert media.pp_stage("ModifyChapters", cutting="chapters") == "Removing chapters…"
    assert media.pp_stage("ForceRecode") == "Re-encoding. This can take a while…"
    assert media.pp_stage("MoveFiles") == "Saving…"
    assert media.pp_stage("FixupM3u8") == "Finishing up…"
    for key in ("EmbedThumbnail", "EmbedSubtitle", "SplitChapters", "Concat", "NormalizeAudio"):
        stage = media.pp_stage(key)
        assert key not in stage and stage.endswith("…") and "—" not in stage


def test_video_presets_are_listed():
    assert list(media.VIDEO_PRESETS)[:7] == ["best", "2160", "1440", "1080", "720", "480", "360"]
    assert "smallest" in media.VIDEO_PRESETS


def test_has_video_and_list():
    assert media._has_video_and_list("https://www.youtube.com/watch?v=abc&list=PL1")
    assert media._has_video_and_list("https://youtu.be/abc?list=PL1")
    assert not media._has_video_and_list("https://www.youtube.com/playlist?list=PL1")
    assert not media._has_video_and_list("https://www.youtube.com/watch?v=abc")
