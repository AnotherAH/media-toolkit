"""Audio decoding for Whisper, done by the bundled ffmpeg in its own process.

faster-whisper decodes files with PyAV, whose wheels carry an FFmpeg built
with the GPL x264/x265 encoders. Loading that into the same process as the
proprietary CUDA and Intel OpenMP runtimes that CTranslate2 needs would make
the installed app one combined work that no licence can cover. So PyAV is left
out of the frozen app, ffmpeg.exe (a separate program) turns the file into
16 kHz mono float samples on a pipe, and faster-whisper only ever receives a
numpy array.

install_av_stub() keeps `import faster_whisper` working when PyAV is absent:
faster-whisper imports `av` at the top of a module but only uses it to decode
files, which we never ask it to do.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
import types
from collections import deque
from pathlib import Path

import numpy as np

from . import config

SAMPLE_RATE = 16000
_CHUNK = 1 << 20                        # bytes read from the pipe at a time
# A reported length beyond this is a broken header, not a recording, and is
# not used to size the buffer up front.
_MAX_TRUSTED_SECONDS = 24 * 3600
_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


class DecodeCancelled(Exception):
    """The check callback asked decoding to stop."""


class _Samples:
    """Float32 samples written straight from the pipe into one array.

    ffmpeg prints the input's length before it writes any audio, so once the
    first piece has arrived the array is sized for the whole file in one step:
    a three-hour lecture costs its 4 bytes per sample and is never copied
    again. Without a known length (some streams) it grows by half when full.
    """

    def __init__(self) -> None:
        self.data = np.empty(0, dtype=np.float32)
        self.used = 0                               # bytes written so far

    def _reserve(self, nbytes: int) -> None:
        if nbytes <= self.data.nbytes:
            return
        grown = np.empty((nbytes + 3) // 4, dtype=np.float32)
        if self.used:
            grown.view(np.uint8)[:self.used] = self.data.view(np.uint8)[:self.used]
        self.data = grown

    def read_from(self, stream, expected_bytes: int) -> int:
        """Read one piece from stream. Returns the bytes read, 0 at the end."""
        if self.data.nbytes - self.used < 4096:
            sized = False
            if expected_bytes >= self.used + _CHUNK:
                try:
                    self._reserve(expected_bytes)
                    sized = True
                except MemoryError:
                    # A damaged header can claim any length; grow as for a
                    # stream instead of failing a file that may be short.
                    pass
            if not sized:
                self._reserve(max(self.used + _CHUNK, int(self.data.nbytes * 1.5), 2 * _CHUNK))
        size = min(_CHUNK, self.data.nbytes - self.used)
        window = memoryview(self.data.view(np.uint8))[self.used:self.used + size]
        try:
            n = stream.readinto(window) or 0
        finally:
            window.release()
        self.used += n
        return n

    def result(self) -> np.ndarray:
        count = self.used // 4
        if count == 0:
            return np.zeros(0, dtype=np.float32)
        if self.data.size - count > max(count // 100, 4096):
            # The length ffmpeg reported was generous: give the tail back.
            try:
                self.data.resize(count, refcheck=False)
            except (ValueError, BufferError):
                return self.data[:count].copy()
        return self.data[:count]


def ffmpeg_path() -> str | None:
    """The bundled ffmpeg, or a system one as a last resort."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if (d := config.ffmpeg_dir()):
        return str(Path(d) / exe)
    return shutil.which("ffmpeg")


def _error(code: str, detail: str = "") -> Exception:
    from .errors import AppError
    return AppError(code, detail)


def decode(path: str, sampling_rate: int = SAMPLE_RATE, check=None,
           on_progress=None) -> np.ndarray:
    """Decode any audio or video file into a float32 mono array.

    Reads the pipe in 1 MB pieces straight into the array that is returned
    (see _Samples), so a long file costs about 4 bytes per sample. check()
    runs between pieces: return True, or raise, to stop, and ffmpeg is
    killed at once. on_progress(fraction) reports how far through the file
    decoding is, when ffmpeg has said how long it is.
    """
    exe = ffmpeg_path()
    if not exe:
        raise _error("ffmpeg_missing", "ffmpeg not found: it is needed to read audio")
    if not Path(path).is_file():
        raise _error("unreadable_file", f"No such file: {path}")

    cmd = [exe, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
           "-threads", "0", "-i", str(path),
           "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(int(sampling_rate)),
           "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    tail: deque[str] = deque(maxlen=25)
    duration = [0.0]

    def drain() -> None:
        # stderr must be read continuously or ffmpeg blocks once the pipe fills.
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not duration[0] and (m := _DURATION.search(line)):
                h, mnt, s = m.groups()
                duration[0] = int(h) * 3600 + int(mnt) * 60 + float(s)
            if line:
                tail.append(line)

    reader = threading.Thread(target=drain, daemon=True, name="ffmpeg-stderr")
    reader.start()

    buf = _Samples()
    try:
        while True:
            if check is not None and check():
                raise DecodeCancelled()
            # One second of headroom over the reported length absorbs rounding.
            known = 0 < duration[0] <= _MAX_TRUSTED_SECONDS
            expected = int((duration[0] + 1.0) * sampling_rate) * 4 if known else 0
            if not buf.read_from(proc.stdout, expected):
                break
            if on_progress and duration[0]:
                done = buf.used / 4 / sampling_rate
                on_progress(min(done / duration[0], 1.0))
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        reader.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

    if proc.returncode != 0 and buf.used < 4:
        detail = "\n".join(tail) or f"ffmpeg exited with code {proc.returncode}"
        raise _error("unreadable_file", detail)
    # A truncated file still yields what could be read, as PyAV did.
    return buf.result()


def duration_of(samples: np.ndarray, sampling_rate: int = SAMPLE_RATE) -> float:
    return float(len(samples)) / sampling_rate if sampling_rate else 0.0


# ----------------------------------------------------------------- PyAV stub

class _MissingAV(types.ModuleType):
    """Stands in for PyAV. Any real use fails loudly instead of mysteriously."""

    def __getattr__(self, attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        raise AttributeError(
            f"av.{attr}: PyAV is not part of Media Toolkit; audio is decoded with ffmpeg")


def install_av_stub(force: bool = False) -> bool:
    """Put an empty `av` module in sys.modules when PyAV is not importable.

    Checks for PyAV without importing it, so a source checkout (where PyAV is
    installed) does not load it at startup either. force=True replaces a PyAV
    that is present but broken. Returns True when the stub was installed.
    """
    current = sys.modules.get("av")
    if isinstance(current, _MissingAV):
        return True
    if current is not None and not force:
        return False
    if not force:
        try:
            if importlib.util.find_spec("av") is not None:
                return False
        except (ImportError, ValueError):
            pass
    for key in [k for k in sys.modules if k == "av" or k.startswith("av.")]:
        sys.modules.pop(key, None)
    stub = _MissingAV("av", "Placeholder: Media Toolkit decodes audio with ffmpeg.")
    stub.__spec__ = importlib.machinery.ModuleSpec("av", loader=None)
    stub.__version__ = "0"
    sys.modules["av"] = stub
    return True


def stubbed() -> bool:
    return isinstance(sys.modules.get("av"), _MissingAV)
