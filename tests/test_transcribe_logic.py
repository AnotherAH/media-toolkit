"""Transcript logic that needs no network: caption track choice, file names,
model catalog and downloads, hardware truth, and the job flow."""
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app import audio, captions, config, hardware, jobs, models, subs, transcribe
from app.subs import Segment as S


# ------------------------------------------------------------- caption tracks

def _track(key, translated=False, exts=("json3", "vtt")):
    q = f"&tlang={key}" if translated else ""
    return [{"ext": e, "url": f"https://example.com/api/timedtext?lang=x&fmt={e}{q}"}
            for e in exts]


def _info(language="", manual=(), auto=(), translated=()):
    return {
        "language": language,
        "subtitles": {k: _track(k) for k in manual},
        "automatic_captions": {**{k: _track(k) for k in auto},
                               **{k: _track(k, True) for k in translated}},
    }


def _keys(choices):
    return [(c.key, c.source) for c in choices]


def test_uploader_captions_in_the_video_language_come_first():
    info = _info("en", manual=["en", "de"], auto=["en-orig", "en"], translated=["fr"])
    assert _keys(captions.candidates(info))[0] == ("en", "official")


def test_spanish_video_with_only_auto_captions_never_gets_the_english_translation():
    # CRIT-3: the default used to pick YouTube's machine-translated 'en'.
    info = _info("es", auto=["es-orig", "es"], translated=["en", "fr"])
    got = _keys(captions.candidates(info))
    assert got[0] == ("es-orig", "auto")
    assert all(k not in ("en", "fr") for k, _ in got)
    assert captions.note_for(captions.candidates(info)[0]) == "automatic Spanish"


def test_unrelated_manual_language_is_not_a_fallback():
    # TR-4: ar/de/ru manual captions for an English request -> Whisper instead.
    info = _info("", manual=["ar", "de", "ru"])
    assert captions.candidates(info, "en") == []
    assert captions.note_for(None, "en", info) == "none in English"


def test_unknown_language_uses_a_single_uploader_track_or_the_original():
    assert _keys(captions.candidates(_info("", manual=["de"]))) == [("de", "official")]
    assert captions.candidates(_info("", manual=["ar", "de"])) == []
    info = _info("", manual=["ar", "de"], auto=["ja-orig", "ja"], translated=["en"])
    assert _keys(captions.candidates(info)) == [("ja-orig", "auto")]


def test_chosen_language_without_its_captions_goes_to_whisper():
    info = _info("en", manual=["en"], auto=["en-orig"], translated=["fa"])
    assert captions.candidates(info, "fa") == []
    assert captions.note_for(None, "fa", info) == "none in Persian"


def test_region_variants_match_and_the_bare_code_wins():
    info = _info("en", manual=["en-GB", "en", "en-US"])
    assert captions.candidates(info)[0].key == "en"
    info = _info("pt", manual=["pt-BR"])
    choice = captions.candidates(info)[0]
    assert choice.key == "pt-BR" and choice.caption_lang == "pt-BR"


def test_live_chat_is_not_a_caption_track():
    info = _info("", manual=["live_chat"])
    assert captions.candidates(info) == []


def test_translate_prefers_uploader_english_then_translations_flagged():
    info = _info("es", manual=["es"], auto=["es-orig", "es"], translated=["en", "en-es"])
    got = captions.candidates(info, "", translate=True)
    assert [c.key for c in got][:2] == ["en-es", "en"]
    assert all(c.source == "auto_translated" for c in got)
    assert got[0].source_lang == "es" and got[0].caption_lang == "en"
    assert captions.note_for(got[0]) == "automatic translation from Spanish"

    info = _info("es", manual=["es", "en"], translated=["en-es"])
    assert _keys(captions.candidates(info, "", translate=True))[0] == ("en", "official")

    info = _info("es", manual=["es"], auto=["es-orig"])
    assert captions.candidates(info, "", translate=True) == []


def test_official_note():
    info = _info("en", manual=["en"])
    assert captions.note_for(captions.candidates(info)[0]) == "English, from the uploader"


def test_primary_codes():
    assert captions.primary("pt-BR") == "pt"
    assert captions.primary("zh-Hans") == "zh"
    assert captions.primary("en-orig") == "en"
    assert captions.primary("iw") == "he"
    assert captions.primary(None) == ""


class _FakeYDL:
    def __init__(self, responses):
        self.responses = responses
        self.opened = []

    def urlopen(self, req):
        url = req.url
        self.opened.append(url)
        result = self.responses(url)
        if isinstance(result, Exception):
            raise result
        import io
        return io.BytesIO(result.encode())

    def _parse_impersonate_targets(self, value):
        return None, []


def test_rate_limited_track_moves_on_without_trying_every_format():
    from yt_dlp.networking.exceptions import HTTPError

    class Resp:
        status = 429
        reason = "Too Many Requests"
        headers = {}
        url = "x"

        def close(self):
            pass

    choice = captions.candidates(_info("es", auto=["es-orig"]))[0]
    ydl = _FakeYDL(lambda url: HTTPError(Resp()))
    with pytest.raises(captions.RateLimited):
        captions.fetch(ydl, choice)
    assert len(ydl.opened) == 1


def test_fetch_falls_back_between_formats():
    raw = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]}]})
    choice = captions.candidates(_info("en", manual=["en"]))[0]
    ydl = _FakeYDL(lambda url: OSError("boom") if "json3" in url else
                   "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n")
    assert [s.text for s in captions.fetch(ydl, choice)] == ["hello"]
    ydl = _FakeYDL(lambda url: raw)
    assert [s.text for s in captions.fetch(ydl, choice)] == ["hi"]


# ------------------------------------------------------------------ file names

def test_one_stem_per_transcript(tmp_path):
    assert transcribe.unique_stem(tmp_path, "Title") == "Title"
    (tmp_path / "Title.txt").write_text("x")
    assert transcribe.unique_stem(tmp_path, "Title") == "Title (2)"
    (tmp_path / "Title (2).mt.json").write_text("{}")
    assert transcribe.unique_stem(tmp_path, "Title") == "Title (3)"
    (tmp_path / "Other.srt").write_text("x")
    assert transcribe.unique_stem(tmp_path, "Other") == "Other (2)"


@pytest.mark.parametrize("title,want", [
    ('a/b\\c|d*e?f"g"<h>', "a-b-c-def'g'h"),
    ("Part 1: Intro?", "Part 1 - Intro"),
    ("¿Qué es un agujero negro? Preguntamos", "¿Qué es un agujero negro Preguntamos"),
    ("  lots   of\tspace\n", "lots of space"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("...", "transcript"),
    ("", "transcript"),
    ("سلام دنیا", "سلام دنیا"),
])
def test_safe_stem(title, want):
    assert transcribe._safe_stem(title) == want


def test_strip_extension_only_when_it_looks_like_one():
    assert transcribe._strip_ext("My talk.mp4") == "My talk"
    assert transcribe._strip_ext("Dr. Smith lecture") == "Dr. Smith lecture"
    assert transcribe._strip_ext("clip") == "clip"


# ------------------------------------------------------------------- models

@pytest.mark.parametrize("name", ["x/..\\..\\..\\victim", "../../x", "C:\\Windows", "",
                                  "tiny/../../x", None, "Systran/faster-whisper-tiny"])
def test_model_names_outside_the_catalog_are_refused(name):
    with pytest.raises(ValueError):
        models.model_dir(name)
    with pytest.raises(ValueError):
        models.purge(name)
    with pytest.raises(ValueError):
        models.ensure(name)


def test_catalog_matches_faster_whisper():
    from faster_whisper.utils import _MODELS
    assert models.REPOS == _MODELS
    for entry in models.CATALOG:
        assert entry["id"] in models.REPOS


def test_model_dirs_stay_inside_the_models_folder():
    root = models.cache_root().resolve()
    for name in models.REPOS:
        assert root in models.model_dir(name).resolve().parents


def test_labels_and_translation_ability():
    assert models.label("turbo") == "Large v3 Turbo"
    assert models.label("large-v2") == "Large v2"
    assert models.label("tiny.en") == "Tiny (English only)"
    assert not models.can_translate("large-v3-turbo")
    assert not models.can_translate("turbo")
    assert not models.can_translate("distil-large-v3")
    assert not models.can_translate("small.en")
    assert models.can_translate("large-v3") and models.can_translate("medium")


def _fake_model(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text("{}")
    (path / "tokenizer.json").write_text("{}")
    (path / "model.bin").write_bytes(b"\0" * (models.MIN_MODEL_BYTES + 1))


def test_installed_rows_use_labels_and_group_aliases():
    target = models.model_dir("large-v3-turbo")
    _fake_model(target)
    try:
        rows = {r["name"]: r for r in models.installed()}
        row = rows["large-v3-turbo"]
        assert row["label"] == "Large v3 Turbo" and row["ok"]
        assert "turbo" in row["aliases"] and "turbo" not in rows
        assert row["download_mb"] == 1550
        assert models.local_path("turbo") == str(target)
    finally:
        models.purge("large-v3-turbo")
    assert models.local_path("turbo") is None


class _Stop(Exception):
    pass


@pytest.fixture
def fake_download(monkeypatch):
    """Replace the network with a slow fake that writes a valid model."""
    gate = threading.Event()
    calls = []

    def fake(name, dest, abort=None):
        calls.append(name)
        (dest / "config.json").write_text("{}")
        (dest / "tokenizer.json").write_text("{}")
        for _ in range(100):
            if abort is not None and abort.is_set():
                raise models._Aborted()
            if gate.wait(0.02):
                break
        (dest / "model.bin").write_bytes(b"\0" * (models.MIN_MODEL_BYTES + 1))

    monkeypatch.setattr(models, "_download_files", fake)
    monkeypatch.setattr(models, "expected_bytes", lambda name: 1000)
    monkeypatch.setattr(models, "_sync_proxy", lambda: None)
    yield gate, calls
    for name in ("tiny", "base", "small"):
        models.purge(name)


def test_cancelled_waiter_stops_an_unshared_download(fake_download):
    gate, calls = fake_download
    start = time.time()

    def check():
        if time.time() - start > 0.2:
            raise _Stop()

    with pytest.raises(_Stop):
        models.ensure("base", check=check)
    for _ in range(100):
        if not models.download_state():
            break
        time.sleep(0.02)
    assert models.local_path("base") is None
    assert not models.model_dir("base").exists()


def test_shared_download_survives_one_cancel(fake_download):
    gate, calls = fake_download
    results = {}

    def keeper():
        results["path"] = models.ensure("small")

    t = threading.Thread(target=keeper)
    t.start()
    time.sleep(0.1)
    start = time.time()

    def check():
        if time.time() - start > 0.1:
            raise _Stop()

    with pytest.raises(_Stop):
        models.ensure("small", check=check)
    gate.set()
    t.join(5)
    assert results["path"] and models.local_path("small")
    assert calls == ["small"]


def test_background_download_needs_no_waiter(fake_download):
    gate, calls = fake_download
    state = models.start_download("tiny")
    assert state["busy"]
    gate.set()
    for _ in range(200):
        if models.local_path("tiny"):
            break
        time.sleep(0.02)
    assert models.local_path("tiny")
    assert not models.start_download("tiny")["busy"]


# ----------------------------------------------------------------- hardware

@pytest.fixture
def fake_gpu(monkeypatch):
    state = {"gpus": [{"name": "NVIDIA GeForce RTX 4070", "compute_cap": 8.9, "vram_mb": 12282}],
             "cuda": 1, "cublas": "C:\\cuda"}
    monkeypatch.setattr(hardware, "probe_gpus", lambda: [dict(g) for g in state["gpus"]])
    monkeypatch.setattr(hardware, "cuda_device_count", lambda: state["cuda"])
    monkeypatch.setattr(hardware, "cublas_dir", lambda: state["cublas"])
    return state


def test_gpu_ready_needs_cublas(fake_gpu):
    s = hardware.summary()
    assert s["gpu_ready"] and s["nvidia_name"] == "NVIDIA GeForce RTX 4070"
    assert s["recommended_model"] == "large-v3-turbo"
    assert hardware.candidates("large-v3-turbo")[0][0] == "cuda"

    fake_gpu["cublas"] = ""
    s = hardware.summary()
    assert not s["gpu_ready"] and not s["cuda"]
    assert s["recommended_model"] == "small"
    assert "processor" in s["note"] and "NVIDIA GeForce RTX 4070" in s["note"]
    assert all(dev == "cpu" for dev, _ in hardware.candidates("large-v3-turbo"))


def test_note_never_names_an_architecture(fake_gpu):
    for cublas in ("C:\\cuda", ""):
        fake_gpu["cublas"] = cublas
        note = hardware.summary()["note"]
        for word in ("Blackwell", "Ada", "Ampere", "Turing", "sm_", "Pascal", "—"):
            assert word not in note
    fake_gpu["gpus"] = []
    assert "processor" in hardware.summary()["note"]


def test_cuda_failure_this_session_turns_the_gpu_off_until_rechecked(fake_gpu):
    assert hardware.gpu_ready()
    hardware.mark_cuda_failed()
    try:
        assert not hardware.gpu_ready()
    finally:
        hardware.forget()
    assert hardware.gpu_ready()


@pytest.mark.parametrize("vram,want", [(16000, "large-v3-turbo"), (5000, "large-v3-turbo"),
                                       (4000, "medium"), (2000, "small"), (1000, "base"),
                                       (0, "small")])
def test_recommend_model(vram, want):
    assert hardware.recommend_model(vram) == want


def test_cuda_probe_runs_once_even_from_many_threads(monkeypatch):
    import sys
    import types
    calls = []

    def count():
        calls.append(threading.get_ident())
        time.sleep(0.05)
        return 1

    fake = types.SimpleNamespace(get_cuda_device_count=count)
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    monkeypatch.setattr(hardware, "_cuda_count", None)
    threads = [threading.Thread(target=hardware.cuda_device_count) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    monkeypatch.setattr(hardware, "_cuda_count", None)


# ------------------------------------------------------------------ options

def test_options_normalise_old_and_new_ui(monkeypatch):
    cfg = dict(config.DEFAULTS, whisper_model="large-v3-turbo")
    o = transcribe._options({"language": "translate"}, cfg)
    assert o["translate"] and o["language"] == ""
    o = transcribe._options({"force_whisper": True, "prefer_captions": True}, cfg)
    assert not o["prefer_captions"]
    o = transcribe._options({"model": "../../evil", "formats": ["txt", "flat", "srt", "bogus"]}, cfg)
    assert o["model"] == "large-v3-turbo"
    assert o["formats"] == ["txt", "srt"]
    o = transcribe._options({"beam": "99", "language": "auto"}, cfg)
    assert o["beam"] == 10 and o["language"] == ""


def test_pick_model_for_translation():
    have = {"large-v3"}
    turbo = "large-v3-turbo"
    assert transcribe.pick_model("large-v3-turbo", True, "", have, turbo)[0] == "large-v3"
    name, why = transcribe.pick_model("large-v3-turbo", True, "", set(), turbo)
    assert name == "medium" and "can't translate" in why and "Large v3 Turbo" in why
    # Medium on a processor too: it is what the user loses least by.
    assert transcribe.pick_model("turbo", True, "", set(), "small")[0] == "medium"
    assert transcribe.pick_model("distil-large-v3", True, "", {"large-v3"}, "small")[0] == "large-v3"
    assert transcribe.pick_model("medium", True, "", set(), turbo) == ("medium", "")
    assert transcribe.pick_model("large-v3-turbo", False, "fa", set(), turbo) == ("large-v3-turbo", "")
    name, why = transcribe.pick_model("distil-large-v3", False, "fa", set(), turbo)
    assert name == "large-v3-turbo" and "English" in why
    # Nothing multilingual downloaded: what this PC's graphics memory suits.
    assert transcribe.pick_model("distil-large-v3", False, "fa", set(), "base")[0] == "base"
    assert transcribe.pick_model("distil-large-v3", False, "fa", {"small"}, turbo)[0] == "small"
    assert transcribe.pick_model("distil-large-v3", False, "en", set(), turbo)[0] == "distil-large-v3"


def test_translating_english_speech_keeps_the_chosen_model():
    assert transcribe.needs_translation(True, "es")
    assert transcribe.needs_translation(True, "")          # unknown: translate
    assert not transcribe.needs_translation(True, "en-US")
    assert not transcribe.needs_translation(False, "es")


# ------------------------------------------------------------------ job flow

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    cfg = dict(config.get(), transcript_dir=str(tmp_path / "transcripts"),
               download_dir=str(tmp_path / "downloads"))
    monkeypatch.setattr(config, "get", lambda: dict(cfg))
    # Upload copies and per-job audio folders stay inside this test's folder.
    temp = tmp_path / "mt-temp"
    monkeypatch.setattr(transcribe, "_upload_root", lambda: temp)
    monkeypatch.setattr(jobs, "UPLOAD_ROOT", temp / "uploads")
    return tmp_path


def _fake_whisper(monkeypatch, segments):
    def fake(samples, o, job, language, chosen):
        if job:
            job.begin("model", "Loading the speech model")
            job.end("model")
            job.begin("transcribing", "Transcribing on this PC's processor")
        return list(segments), {"source": "whisper", "caption_lang": "", "source_lang": "en",
                                "language_chosen": chosen, "engine": "Whisper tiny",
                                "device": "cpu", "compute_type": "int8",
                                "realtime_factor": 10.0}
    monkeypatch.setattr(transcribe, "_transcribe", fake)
    monkeypatch.setattr(audio, "decode",
                        lambda path, **kw: np.zeros(16000, dtype=np.float32))


def _upload(name: str, data: bytes = b"RIFF") -> Path:
    folder = jobs.UPLOAD_ROOT / f"test-{time.time_ns()}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "upload.bin"
    path.write_bytes(data)
    return path


def test_local_file_job_writes_one_stem_sidecar_and_drops_the_upload(workspace, monkeypatch):
    _fake_whisper(monkeypatch, [S(0, 1.5, "Hello world."), S(1.5, 3, "Second line.")])
    results = []
    for _ in range(2):
        up = _upload("My talk.mp4")
        job = jobs.create("transcript", "", {"local_path": str(up)}, title="My talk.mp4")
        result = transcribe.run_transcript(job["id"], "", {"local_path": str(up)})
        results.append((job["id"], result))
        assert not up.exists()

    jid, first = results[0]
    assert first["stem"] == "My talk"
    assert results[1][1]["stem"] == "My talk (2)"
    outdir = Path(first["output_dir"])
    for stem in ("My talk", "My talk (2)"):
        for ext in ("txt", "srt", "md", "mt.json"):
            assert (outdir / f"{stem}.{ext}").is_file()
    assert set(first) >= {"meta", "detail", "stats", "formats", "output_dir", "stem",
                          "segments", "text"}
    assert first["text"].startswith("Hello world.")
    side = json.loads((outdir / "My talk.mt.json").read_text("utf-8"))
    assert set(side) >= {"meta", "detail", "stats", "segments"}
    assert side["segments"][0]["text"] == "Hello world."

    job = jobs.get(jid)
    states = {s["key"]: s["state"] for s in job["steps"]}
    assert [s["key"] for s in job["steps"]] == ["reading", "model", "transcribing", "saving"]
    assert job["steps"][0]["label"] == "Reading the file"
    assert states["saving"] == "done"
    kinds = sorted(f["kind"] for f in job["files"])
    assert kinds == ["notes", "subtitles", "transcript"]


def test_a_users_own_file_is_never_deleted(workspace, monkeypatch, tmp_path):
    _fake_whisper(monkeypatch, [S(0, 1, "Hi.")])
    mine = tmp_path / "mine.wav"
    mine.write_bytes(b"RIFF")
    job = jobs.create("transcript", "", {"local_path": str(mine)}, title="mine.wav")
    transcribe.run_transcript(job["id"], "", {"local_path": str(mine)})
    assert mine.exists()


def test_no_speech_is_an_error_and_keeps_the_upload(workspace, monkeypatch):
    _fake_whisper(monkeypatch, [])
    up = _upload("quiet.wav")
    job = jobs.create("transcript", "", {"local_path": str(up)}, title="quiet.wav")
    with pytest.raises(Exception) as info:
        transcribe.run_transcript(job["id"], "", {"local_path": str(up)})
    assert getattr(info.value, "code", "") == "no_speech"
    assert up.exists()
    up.unlink()


def test_captions_path_marks_whisper_steps_skipped(workspace, monkeypatch):
    info = _info("en", manual=["en"])
    info.update(title="Tears", id="abc", uploader="Blender", duration=10,
                webpage_url="https://www.youtube.com/watch?v=abc")
    monkeypatch.setattr(transcribe, "_extract", lambda url, translate: info)
    choice = captions.candidates(info)[0]
    monkeypatch.setattr(transcribe, "_find_captions",
                        lambda url, i, lang, tr, check=None: (choice, [S(0, 1, "Captions text.")],
                                                              captions.note_for(choice), ""))
    job = jobs.create("transcript", info["webpage_url"], {})
    result = transcribe.run_transcript(job["id"], info["webpage_url"],
                                       {"formats": ["txt"], "prefer_captions": True})
    snap = jobs.get(job["id"])
    states = {s["key"]: (s["state"], s["note"]) for s in snap["steps"]}
    assert states["captions"] == ("done", "English, from the uploader")
    for key in ("audio", "model", "transcribing"):
        assert states[key][0] == "skipped"
    d = result["detail"]
    assert d["source"] == "official" and d["caption_lang"] == "en" and d["site"] == "YouTube"
    assert snap["title"] == "Tears" and snap["uploader"] == "Blender"


def test_playlist_links_are_refused(monkeypatch):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"_type": "playlist", "title": "P", "entries": []}

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(Exception) as info:
        transcribe._extract("https://www.youtube.com/playlist?list=x", False)
    assert getattr(info.value, "code", "") == "playlist_not_supported"


# ------------------------------------------------------------ whisper steps

class _Info:
    language = "es"
    language_probability = 0.97


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _FakeModel:
    def __init__(self):
        self.boom = None
        self.kwargs = None

    def transcribe(self, samples, **kwargs):
        # LEG-1: Whisper only ever gets decoded samples, never a file path.
        assert isinstance(samples, np.ndarray) and samples.dtype == np.float32
        self.kwargs = kwargs
        if self.boom:
            raise self.boom
        return iter([_Seg(0, 1.0, " Hola."), _Seg(1.0, 9.0, " Adios.")]), _Info()


@pytest.fixture
def fake_engine(monkeypatch):
    """Whisper without weights: downloads, loads and runs are all fakes."""
    state = {"model": _FakeModel(), "installed": set(), "loaded": [], "on_ensure": None}

    def ensure(name, on_progress=None, check=None, on_status=None):
        if state["on_ensure"]:
            state["on_ensure"]()
        if check:
            check()
        if on_progress and name not in state["installed"]:
            on_progress(0, 100 * 1048576)
            on_progress(100 * 1048576, 100 * 1048576)
        state["installed"].add(name)
        return "fake-path"

    def load(name, device="auto", compute="auto", check=None):
        state["loaded"].append(name)
        return state["model"], "cpu", "int8"

    monkeypatch.setattr(models, "ensure", ensure)
    monkeypatch.setattr(models, "local_path",
                        lambda name: "fake-path" if name in state["installed"] else None)
    monkeypatch.setattr(models, "installed", lambda: [
        {"name": n, "ok": True, "aliases": []} for n in state["installed"]])
    monkeypatch.setattr(transcribe, "load_model", load)
    monkeypatch.setattr(transcribe, "_schedule_idle_unload", lambda: None)
    monkeypatch.setattr(hardware, "gpu_ready", lambda: False)
    return state


def _record_notes(monkeypatch):
    notes = []
    real = jobs.step

    def step(jid, key, state, note=None, label=None):
        if note:
            notes.append((key, note))
        return real(jid, key, state, note=note, label=label)

    monkeypatch.setattr(jobs, "step", step)
    return notes


def test_translate_with_turbo_switches_model_and_says_so(workspace, fake_engine, monkeypatch):
    notes = _record_notes(monkeypatch)
    job = jobs.create("transcript", "", {})
    jobs.set_steps(job["id"], [("model", transcribe.STEP_LABELS["model_download"]),
                               ("transcribing", transcribe.STEP_LABELS["transcribing"])])
    o = transcribe._options({"model": "large-v3-turbo", "language": "translate"}, config.get())
    samples = np.zeros(16000 * 10, dtype=np.float32)
    segs, detail = transcribe._transcribe(samples, o, transcribe._Job(job["id"]), "", False)

    assert fake_engine["loaded"] == ["medium"]
    assert fake_engine["model"].kwargs["task"] == "translate"
    snap = jobs.get(job["id"])
    steps = {s["key"]: s for s in snap["steps"]}
    assert steps["model"]["label"] == "Downloading the speech model (one time only)"
    assert ("model", "0 of 100 MB") in notes
    assert steps["model"]["note"] == "using Medium, since Large v3 Turbo can't translate"
    assert steps["transcribing"]["label"] == "Transcribing on this PC's processor"
    assert snap["stage_detail"] == "medium · CPU int8 · instead of large-v3-turbo"
    assert detail["model"] == "medium" and detail["requested_model"] == "large-v3-turbo"
    assert detail["source"] == "whisper" and detail["source_lang"] == "es"
    assert [s.text for s in segs] == ["Hola.", "Adios."]
    assert segs[-1].end == 9.0


def test_a_model_on_disk_is_loaded_not_downloaded(workspace, fake_engine):
    fake_engine["installed"].add("small")
    job = jobs.create("transcript", "", {})
    o = transcribe._options({"model": "small", "language": "fa"}, config.get())
    _, detail = transcribe._transcribe(np.zeros(16000, dtype=np.float32), o,
                                       transcribe._Job(job["id"]), "fa", True)
    steps = {s["key"]: s for s in jobs.get(job["id"])["steps"]}
    assert steps["model"]["label"] == "Loading the speech model"
    assert fake_engine["model"].kwargs["language"] == "fa"
    assert detail["language_chosen"] is True and detail["model_note"] == ""


def test_a_failure_marks_the_running_step_failed(workspace, fake_engine, monkeypatch):
    fake_engine["model"].boom = RuntimeError("decoder exploded")
    monkeypatch.setattr(audio, "decode", lambda path, **kw: np.zeros(16000, dtype=np.float32))
    up = _upload("x.wav")
    job = jobs.create("transcript", "", {"local_path": str(up)}, title="x.wav")
    with pytest.raises(RuntimeError):
        transcribe.run_transcript(job["id"], "", {"local_path": str(up)})
    states = {s["key"]: s["state"] for s in jobs.get(job["id"])["steps"]}
    assert states["transcribing"] == "failed"
    assert states["saving"] == "pending"
    assert up.exists()                          # kept, so Try again works


def test_cancel_during_the_model_download_leaves_no_step_spinning(workspace, fake_engine,
                                                                  monkeypatch):
    monkeypatch.setattr(audio, "decode", lambda path, **kw: np.zeros(16000, dtype=np.float32))
    up = _upload("x.wav")
    job = jobs.create("transcript", "", {"local_path": str(up)}, title="x.wav")
    fake_engine["on_ensure"] = lambda: jobs.cancel(job["id"])
    with pytest.raises(jobs.Cancelled):
        transcribe.run_transcript(job["id"], "", {"local_path": str(up)})
    states = {s["key"]: s["state"] for s in jobs.get(job["id"])["steps"]}
    assert states["model"] == "skipped"
    assert "active" not in states.values()


class _CaptionYDL:
    """A YoutubeDL stand-in whose caption requests all answer 429."""

    def __init__(self, opts=None):
        self.opened = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def urlopen(self, req):
        from yt_dlp.networking.exceptions import HTTPError

        class Resp:
            status = 429
            reason = "Too Many Requests"
            headers = {}
            url = req.url

            def close(self):
                pass

        self.opened.append(req.url)
        raise HTTPError(Resp())

    def _parse_impersonate_targets(self, value):
        return None, []


def test_captions_that_fail_fall_through_to_whisper_and_say_so(workspace, fake_engine,
                                                               monkeypatch):
    import yt_dlp
    from app import media
    info = _info("es", auto=["es-orig", "es"])
    info.update(title="Clip", id="c1", uploader="Uploader", duration=10,
                webpage_url="https://www.youtube.com/watch?v=c1")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _CaptionYDL)
    monkeypatch.setattr(media, "base_opts", lambda *a, **k: {})
    monkeypatch.setattr(transcribe, "_extract", lambda url, translate: info)

    choice, segs, note, error = transcribe._find_captions(info["webpage_url"], info, "", False)
    assert choice is None and segs == [] and note == transcribe.NOTE_CAPTIONS_FAILED
    assert "es-orig" in error and "es:" in error and "429" in error
    assert "https://" not in error              # signed caption URLs never reach the log

    def fake_audio(url, info, workdir, hook, check=None):
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / "audio.webm"
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(transcribe, "_download_audio", fake_audio)
    monkeypatch.setattr(audio, "decode", lambda path, **kw: np.zeros(16000, dtype=np.float32))
    job = jobs.create("transcript", info["webpage_url"], {})
    result = transcribe.run_transcript(job["id"], info["webpage_url"],
                                       {"model": "tiny", "formats": ["txt"]})
    steps = {s["key"]: s for s in jobs.get(job["id"])["steps"]}
    assert steps["captions"]["state"] == "done"
    assert steps["captions"]["note"] == "couldn't load captions, transcribing instead"
    assert result["detail"]["source"] == "whisper"
    assert "429" in result["detail"]["caption_error"]
    # The video's own language went to Whisper, since none was chosen.
    assert fake_engine["model"].kwargs["language"] == "es"
    assert not list((workspace / "mt-temp").glob("j*_*"))   # per-job audio folder removed


def test_mb_note():
    assert transcribe._mb_note(851_443_712, 1_625_292_800) == "812 of 1,550 MB"
    assert transcribe._mb_note(3 * 1048576, 0) == "3 MB"


def test_idle_unload_never_pulls_a_model_from_under_a_run(monkeypatch):
    monkeypatch.setattr(transcribe, "_loaded", (("p", "cpu", "int8"), object()))
    with transcribe._run_lock:
        transcribe._idle_unload()
        assert transcribe._loaded is not None
    transcribe._idle_unload()
    assert transcribe._loaded is None


# ------------------------------------------------------------ models extras

def test_option_labels_match_the_copy():
    assert [m["option_label"] for m in models.catalog()] == [
        "Large v3 Turbo · best balance · 1.6 GB",
        "Large v3 · most accurate, slower · 3.1 GB",
        "Distil Large v3 · English only, fast · 1.5 GB",
        "Medium · 1.5 GB",
        "Small · good on a processor · 480 MB",
        "Base · 145 MB",
        "Tiny · 75 MB",
    ]


def test_purge_refuses_while_a_download_holds_the_lock():
    target = models.model_dir("tiny")
    _fake_model(target)
    try:
        # Another copy of the app downloading holds this lock.
        with models._file_lock(models._lock_path(target), None):
            with pytest.raises(models.ModelBusy):
                models.purge("tiny")
            assert target.is_dir()
        assert issubclass(models.ModelBusy, RuntimeError)
    finally:
        models.purge("tiny")
    assert not target.exists()


def test_ensure_reports_milestones_for_the_diagnosis(fake_download):
    gate, calls = fake_download
    msgs = []
    threading.Timer(0.2, gate.set).start()
    models.ensure("tiny", on_status=msgs.append)
    assert any(m.startswith("Downloading Tiny") for m in msgs)
    assert msgs[-1] == "Tiny is ready"
    models.ensure("tiny", on_status=msgs.append)
    assert msgs[-1] == "Tiny is already downloaded"


def test_model_downloads_follow_a_changed_proxy(monkeypatch):
    import huggingface_hub
    closed = []
    monkeypatch.setattr(huggingface_hub, "close_session", lambda: closed.append(1))
    monkeypatch.setattr(models, "_proxy_seen", None)
    for var in models._PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    models._sync_proxy()
    before = len(closed)
    models._sync_proxy()
    assert len(closed) == before                # unchanged: keep the client
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    models._sync_proxy()
    assert len(closed) == before + 1            # changed: rebuild it


# ---------------------------------------------------------- hardware extras

def test_gpu_pack_alone_makes_the_gpu_ready(fake_gpu, monkeypatch):
    fake_gpu["cublas"] = ""
    monkeypatch.setattr(hardware, "gpu_pack_installed", lambda: True)
    assert hardware.gpu_ready()


def test_backend_cache_reads_a_bom_and_writes_atomically(monkeypatch, tmp_path):
    path = tmp_path / ".backend-cache.json"
    monkeypatch.setattr(hardware, "CACHE_PATH", path)
    path.write_text('﻿{"tiny": {"device": "cpu", "compute_type": "int8"}}', "utf-8")
    assert hardware.recall("tiny") == ("cpu", "int8")
    hardware.remember("base", "cuda", "float16")
    data = json.loads(path.read_text("utf-8"))
    assert data["base"] == {"device": "cuda", "compute_type": "float16"}
    assert data["tiny"]["device"] == "cpu"
    assert not path.with_name(path.name + ".tmp").exists()
    path.write_text("{ broken", "utf-8")
    assert hardware.recall("tiny") is None


def test_switching_models_releases_the_previous_one(monkeypatch):
    import weakref
    alive = []

    class FakeWhisper:
        def __init__(self, path, **kw):
            self.path = path
            alive.append(weakref.ref(self))

    monkeypatch.setattr(transcribe, "_whisper_class", lambda: FakeWhisper)
    monkeypatch.setattr(transcribe, "_warmup", lambda model: None)
    monkeypatch.setattr(models, "ensure", lambda name, **kw: f"dir-{models.canonical(name)}")
    monkeypatch.setattr(hardware, "candidates", lambda *a, **k: [("cpu", "int8")])
    monkeypatch.setattr(hardware, "recall", lambda name: None)
    monkeypatch.setattr(hardware, "remember", lambda *a: None)
    monkeypatch.setattr(transcribe, "_loaded", None)

    first, _, _ = transcribe.load_model("tiny")
    again, _, _ = transcribe.load_model("tiny")
    assert again is first                        # same model: reused, not reloaded
    del first, again
    second, _, _ = transcribe.load_model("base")
    assert second.path == "dir-base"
    assert [r() is not None for r in alive] == [False, True]   # tiny was let go
    transcribe.unload()


@pytest.mark.network
def test_real_official_captions_read_clean():
    # T-05 acceptance on Tears of Steel (CC-BY), which has uploader captions.
    url = "https://www.youtube.com/watch?v=R6MlUcmOul8"
    info = transcribe._extract(url, False)
    choice, segs, note, error = transcribe._find_captions(url, info, "", False)
    assert choice is not None and choice.source == "official", error
    assert captions.primary(choice.caption_lang) == "en"
    assert note == "English, from the uploader"
    paragraphs = subs.to_txt(segs).split("\n\n")
    assert all("\n" not in p for p in paragraphs)
    assert all(len(p) >= 250 for p in paragraphs[:-1])
