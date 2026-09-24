"""Audio decoding through the bundled ffmpeg, and the PyAV placeholder."""
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from app import audio, config

needs_ffmpeg = pytest.mark.skipif(not (config.BIN_DIR / "ffmpeg.exe").exists()
                                  and not audio.ffmpeg_path(),
                                  reason="bin/ffmpeg.exe is missing")


def _make(tmp_path, name, *args):
    path = tmp_path / name
    subprocess.run([audio.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args,
                    str(path)], check=True, stdin=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path


@needs_ffmpeg
def test_decode_sine_to_16k_mono_float(tmp_path):
    wav = _make(tmp_path, "sine.wav", "-f", "lavfi",
                "-i", "sine=frequency=440:sample_rate=44100:duration=3", "-ac", "2")
    seen = []
    samples = audio.decode(str(wav), on_progress=seen.append)
    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert abs(len(samples) - 48000) <= 160
    assert 0.1 < float(np.abs(samples).max()) < 0.2       # lavfi sine is 1/8 amplitude
    assert audio.duration_of(samples) == pytest.approx(3.0, abs=0.02)
    assert all(0 <= f <= 1 for f in seen)


@needs_ffmpeg
def test_decode_non_ascii_path(tmp_path):
    wav = _make(tmp_path, "صدا تست.wav", "-f", "lavfi", "-i", "sine=duration=1")
    assert len(audio.decode(str(wav))) == pytest.approx(16000, abs=160)


@needs_ffmpeg
def test_decode_file_without_audio_is_unreadable(tmp_path):
    mp4 = _make(tmp_path, "video.mp4", "-f", "lavfi", "-i", "testsrc=duration=1:size=32x32:rate=5")
    with pytest.raises(Exception) as info:
        audio.decode(str(mp4))
    assert getattr(info.value, "code", "") == "unreadable_file"


@needs_ffmpeg
def test_decode_garbage_is_unreadable(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio at all" * 10)
    with pytest.raises(Exception) as info:
        audio.decode(str(bad))
    assert getattr(info.value, "code", "") == "unreadable_file"


@needs_ffmpeg
def test_decode_can_be_cancelled(tmp_path):
    wav = _make(tmp_path, "long.wav", "-f", "lavfi", "-i", "sine=duration=60")
    with pytest.raises(audio.DecodeCancelled):
        audio.decode(str(wav), check=lambda: True)

    class Stop(Exception):
        pass

    def stop():
        raise Stop()

    with pytest.raises(Stop):
        audio.decode(str(wav), check=stop)


def test_missing_file_is_unreadable(tmp_path):
    if not audio.ffmpeg_path():
        pytest.skip("ffmpeg missing")
    with pytest.raises(Exception) as info:
        audio.decode(str(tmp_path / "nope.wav"))
    assert getattr(info.value, "code", "") == "unreadable_file"


def _run(code: str) -> str:
    out = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True,
                         text=True, cwd=str(config.ROOT), timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_stub_only_when_pyav_is_missing():
    # PyAV is installed in the development venv: the stub must stay out of the way,
    # and checking must not import PyAV.
    got = _run("""
        import sys
        from app import audio
        print(audio.install_av_stub(), 'av' in sys.modules)
    """)
    assert got == "False False"


def test_faster_whisper_imports_and_takes_arrays_with_the_stub():
    # What the frozen build does: no PyAV at all.
    got = _run("""
        import sys, importlib.util
        real = importlib.util.find_spec
        importlib.util.find_spec = lambda name, *a: None if name == 'av' else real(name, *a)
        from app import audio
        assert audio.install_av_stub()
        importlib.util.find_spec = real
        import faster_whisper
        from faster_whisper.audio import decode_audio
        print(audio.stubbed(), any(k.startswith('av.') for k in sys.modules))
        try:
            sys.modules['av'].open
        except AttributeError as exc:
            print('ffmpeg' in str(exc))
    """)
    assert got.splitlines() == ["True False", "True"]


# ------------------------------------------------------------ sample buffer

class _Trickle:
    """A pipe that hands out a few bytes at a time, never 4-byte aligned."""

    def __init__(self, data: bytes, step: int = 7):
        self.data, self.pos, self.step = data, 0, step

    def readinto(self, view):
        n = min(len(view), self.step, len(self.data) - self.pos)
        view[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n


def test_samples_buffer_reassembles_unaligned_pieces():
    data = np.linspace(-1, 1, 5001, dtype=np.float32)
    buf = audio._Samples()
    stream = _Trickle(data.tobytes())
    while buf.read_from(stream, 0):
        pass
    assert np.array_equal(buf.result(), data)


def test_samples_buffer_is_sized_once_when_the_length_is_known():
    import io
    data = np.arange(3_000_000, dtype=np.float32)          # 12 MB, several pieces
    expected = data.nbytes + 16000 * 4
    buf = audio._Samples()
    stream = io.BytesIO(data.tobytes())
    sizes = set()
    while buf.read_from(stream, expected):
        sizes.add(buf.data.nbytes)
    assert sizes == {expected}                              # never grown, never copied
    assert np.array_equal(buf.result(), data)


def test_samples_buffer_grows_without_a_length_and_trims_the_tail():
    import io
    data = np.ones(1_300_000, dtype=np.float32)
    buf = audio._Samples()
    stream = io.BytesIO(data.tobytes())
    while buf.read_from(stream, 0):
        pass
    out = buf.result()
    assert np.array_equal(out, data)
    base = out.base if out.base is not None else out
    assert base.nbytes <= data.nbytes + 4096 * 4 + data.nbytes // 100


def test_empty_output_is_an_empty_array():
    assert audio._Samples().result().size == 0


@needs_ffmpeg
def test_decode_long_file_costs_about_four_bytes_per_sample(tmp_path):
    wav = _make(tmp_path, "long.wav", "-f", "lavfi", "-i", "sine=duration=600",
                "-ar", "16000")
    samples = audio.decode(str(wav))
    assert abs(len(samples) - 600 * 16000) <= 1600
    base = samples.base if samples.base is not None else samples
    # The array handed to Whisper is the only copy: at most a second of slack.
    assert base.nbytes <= samples.nbytes + 16000 * 4 * 2
