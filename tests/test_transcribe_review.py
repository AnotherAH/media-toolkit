"""Review fixes in the transcript area: live links, cookie copies, step
bookkeeping across threads, English 'translation', model deletion and the
background download that follows a cancelled one. No network."""
import threading
import time

import numpy as np
import pytest

from app import audio, config, hardware, jobs, media, models, transcribe

# Fixtures shared with the main transcript tests (pytest finds them by name).
from tests.test_transcribe_logic import (_fake_model, fake_download,  # noqa: F401
                                         fake_engine, fake_gpu, workspace)


# ------------------------------------------------------------ live links

@pytest.mark.parametrize("status,reason", [("is_live", transcribe.LIVE_NOW),
                                           ("is_upcoming", transcribe.LIVE_LATER)])
def test_a_live_link_ends_skipped_with_its_title(workspace, monkeypatch, status, reason):
    info = {"id": "l1", "title": "ISS live", "uploader": "NASA", "live_status": status,
            "is_live": status == "is_live", "webpage_url": "https://www.youtube.com/watch?v=l1",
            "subtitles": {}, "automatic_captions": {}}
    monkeypatch.setattr(transcribe, "_extract", lambda url, translate: info)
    job = jobs.create("transcript", info["webpage_url"], {})
    with pytest.raises(jobs.Skipped) as exc:
        transcribe.run_transcript(job["id"], info["webpage_url"], {})
    assert exc.value.reason == reason
    assert "—" not in reason
    snap = jobs.get(job["id"])
    assert snap["title"] == "ISS live"
    assert "active" not in {s["state"] for s in snap["steps"]}


def test_a_live_link_through_the_job_runner_is_skipped_not_failed(workspace, monkeypatch):
    info = {"id": "l2", "title": "Stream", "live_status": "is_live", "is_live": True,
            "webpage_url": "https://www.youtube.com/watch?v=l2"}
    monkeypatch.setattr(transcribe, "_extract", lambda url, translate: info)
    job = jobs.create("transcript", info["webpage_url"], {})
    jobs.submit(job["id"], transcribe.run_transcript, info["webpage_url"], {})
    for _ in range(200):
        snap = jobs.get(job["id"])
        if snap["status"] in jobs.FINAL:
            break
        time.sleep(0.02)
    assert snap["status"] == "skipped" and snap["stage"] == transcribe.LIVE_NOW
    assert snap["error"] is None


def test_a_playlist_refusal_carries_its_title(monkeypatch):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            assert self.opts["playlistend"] == 1      # never lists a whole channel
            return {"_type": "playlist", "title": "Talks", "uploader": "Blender",
                    "entries": [{}]}

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(media, "base_opts", lambda *a, **k: {})
    with pytest.raises(Exception) as info:
        transcribe._extract("https://www.youtube.com/@blender", False)
    assert info.value.code == "playlist_not_supported"
    assert info.value.params["title"] == "Talks"


# ------------------------------------------------------------ cookie copies

def test_every_lookup_deletes_its_private_cookie_copy(monkeypatch, tmp_path):
    released = []
    made = []

    def base_opts(*a, **k):
        path = tmp_path / f"cookies-{len(made)}.txt"
        path.write_text("# Netscape HTTP Cookie File\n")
        made.append(str(path))
        return {"cookiefile": str(path)}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "v", "title": "T", "subtitles": {}, "automatic_captions": {}}

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(media, "base_opts", base_opts)
    monkeypatch.setattr(media, "release", lambda opts: released.append(opts["cookiefile"]))

    info = transcribe._extract("https://example.com/v", False)
    info["subtitles"] = {"en": [{"ext": "vtt", "data": "WEBVTT\n\n00:00.000 --> 00:01.000\nHi\n"}]}
    info["language"] = "en"
    choice, segs, _, _ = transcribe._find_captions("https://example.com/v", info, "", False)
    assert choice and segs[0].text == "Hi"
    assert released == made and len(made) == 2


# ------------------------------------------------------------ step bookkeeping

def test_a_late_download_tick_cannot_reactivate_a_finished_step(workspace):
    job = jobs.create("transcript", "", {})
    jobs.set_steps(job["id"], [("model", "Downloading"), ("transcribing", "Transcribing")])
    tracker = transcribe._Job(job["id"])
    tracker.begin("model", "Downloading")
    tracker.stop("skipped")                       # the user cancelled
    assert not tracker.progress(force=True, step="model", note="80 of 100 MB", progress=0.8)
    steps = {s["key"]: s for s in jobs.get(job["id"])["steps"]}
    assert steps["model"]["state"] == "skipped" and steps["model"]["note"] == ""


def test_progress_never_moves_back_within_a_step(workspace):
    job = jobs.create("transcript", "", {})
    tracker = transcribe._Job(job["id"])
    tracker.begin("audio", "Getting the audio")
    tracker.progress(force=True, progress=0.6, indeterminate=False)
    tracker.progress(force=True, progress=0.4, indeterminate=False)     # estimate shrank
    assert jobs.get(job["id"])["progress"] == 0.6
    tracker.progress(force=True, progress=0.0, indeterminate=True)      # now decoding
    tracker.progress(force=True, progress=0.1, indeterminate=False)
    assert jobs.get(job["id"])["progress"] == 0.1
    tracker.begin("transcribing", "Transcribing")
    assert jobs.get(job["id"])["progress"] == 0.0


def test_download_ticks_from_another_thread_race_a_cancel_safely(workspace):
    job = jobs.create("transcript", "", {})
    jobs.set_steps(job["id"], [("model", "Downloading")])
    tracker = transcribe._Job(job["id"])
    tracker.begin("model", "Downloading")
    stop = threading.Event()

    def ticker():
        while not stop.is_set():
            tracker.progress(force=True, step="model", note="1 of 2 MB", progress=0.5)

    t = threading.Thread(target=ticker)
    t.start()
    time.sleep(0.05)
    tracker.stop("skipped")
    time.sleep(0.05)
    stop.set()
    t.join()
    assert jobs.get(job["id"])["steps"][0]["state"] == "skipped"


# ------------------------------------------------------ English "translation"

def test_translate_on_english_speech_keeps_the_model_and_transcribes(workspace, fake_engine):
    job = jobs.create("transcript", "", {})
    o = transcribe._options({"model": "large-v3-turbo", "language": "translate"}, config.get())
    _, detail = transcribe._transcribe(np.zeros(16000, dtype=np.float32), o,
                                       transcribe._Job(job["id"]), "en", False)
    assert fake_engine["loaded"] == ["large-v3-turbo"]      # no 1.5 GB detour
    assert fake_engine["model"].kwargs["task"] == "transcribe"
    assert detail["translated"] is False and detail["model_note"] == ""


# ------------------------------------------------------------ models

def test_delete_reports_files_it_could_not_remove(monkeypatch):
    target = models.model_dir("base")
    _fake_model(target)
    real = models.shutil.rmtree
    try:
        monkeypatch.setattr(models.shutil, "rmtree", lambda *a, **k: None)
        with pytest.raises(OSError) as exc:
            models.purge("base")
        assert "in use" in exc.value.strerror
    finally:
        monkeypatch.setattr(models.shutil, "rmtree", real)
        models.purge("base")
    assert not target.exists()


def test_download_again_waits_out_a_download_that_is_stopping(fake_download):
    gate, calls = fake_download

    def check():
        if calls:                               # cancel once real work has begun
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        models.ensure("tiny", check=check)      # nobody else wants it: it stops
    state = models.start_download("tiny")       # Settings: "Download again now"
    assert state["busy"], state
    gate.set()
    deadline = time.time() + 15
    while time.time() < deadline and not models.local_path("tiny"):
        time.sleep(0.05)
    assert models.local_path("tiny"), f"the new download never finished: {models.download_state()}"
    assert calls == ["tiny", "tiny"]


def _fake_hub(monkeypatch, listing, missing=()):
    import types

    import huggingface_hub
    fetched = []

    class Api:
        def model_info(self, repo, **kw):
            if listing is None:
                raise OSError("offline")
            return types.SimpleNamespace(siblings=[types.SimpleNamespace(rfilename=f)
                                                   for f in listing])

    def download(repo, fname, local_dir=None, tqdm_class=None):
        if fname in missing:
            raise OSError(f"{fname}: connection reset")
        fetched.append(fname)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    return fetched


def test_without_a_listing_the_mel_config_is_still_fetched(monkeypatch, tmp_path):
    fetched = _fake_hub(monkeypatch, None, missing=("vocabulary.txt",))
    models._download_files("large-v3", tmp_path)
    # large-v3 needs 128 mel bands from preprocessor_config.json.
    assert "preprocessor_config.json" in fetched and fetched[-1] == "model.bin"


def test_a_listed_file_that_fails_fails_the_download(monkeypatch, tmp_path):
    _fake_hub(monkeypatch, ["config.json", "model.bin", "tokenizer.json",
                            "preprocessor_config.json", "vocabulary.json", "README.md"],
              missing=("preprocessor_config.json",))
    with pytest.raises(RuntimeError, match="preprocessor_config.json"):
        models._download_files("large-v3", tmp_path)


def test_a_processor_fallback_after_a_cuda_failure_is_not_remembered(monkeypatch):
    remembered = []

    class FakeWhisper:
        def __init__(self, path, device="cpu", **kw):
            if device == "cuda":
                raise RuntimeError("CUDA failed with error out of memory")

    monkeypatch.setattr(transcribe, "_whisper_class", lambda: FakeWhisper)
    monkeypatch.setattr(transcribe, "_warmup", lambda model: None)
    monkeypatch.setattr(models, "ensure", lambda name, **kw: f"dir-{name}")
    monkeypatch.setattr(hardware, "recall", lambda name: None)
    monkeypatch.setattr(hardware, "remember", lambda *a: remembered.append(a))
    monkeypatch.setattr(transcribe, "_loaded", None)

    # Graphics memory full: the processor run is not a verdict on the card.
    monkeypatch.setattr(hardware, "candidates", lambda *a, **k: [("cuda", "float16"),
                                                                  ("cpu", "int8")])
    assert transcribe.load_model("tiny")[1] == "cpu"
    # CUDA broke earlier this session: same.
    monkeypatch.setattr(hardware, "candidates", lambda *a, **k: [("cpu", "int8")])
    monkeypatch.setattr(hardware, "cuda_failed", lambda: True)
    transcribe.unload()
    assert transcribe.load_model("base")[1] == "cpu"
    assert remembered == []
    # A machine that simply has no usable GPU: the processor is the answer.
    monkeypatch.setattr(hardware, "cuda_failed", lambda: False)
    transcribe.unload()
    transcribe.load_model("small")
    assert remembered == [("small", "cpu", "int8")]
    transcribe.unload()


# ------------------------------------------------------------ hardware

def test_summary_does_not_probe_cuda_without_cublas(fake_gpu, monkeypatch):
    fake_gpu["cublas"] = ""
    monkeypatch.setattr(hardware, "gpu_pack_installed", lambda: False)

    def boom():
        raise AssertionError("CUDA probed although cuBLAS is missing")

    monkeypatch.setattr(hardware, "cuda_device_count", boom)
    s = hardware.summary()
    assert s["cuda_driver"] and not s["gpu_ready"]


# ------------------------------------------------------------ audio buffer

class _Pipe:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def readinto(self, view) -> int:
        n = min(len(view), len(self.data) - self.pos)
        view[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n


def test_a_header_claiming_an_absurd_length_does_not_fail_the_file():
    samples = np.arange(50_000, dtype=np.float32)
    pipe = _Pipe(samples.tobytes())
    buf = audio._Samples()
    while buf.read_from(pipe, 1 << 50):         # "a petabyte of audio"
        pass
    assert np.array_equal(buf.result(), samples)
