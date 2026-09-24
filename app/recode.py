"""Postprocessors the app adds to yt-dlp's chain.

Each one fills a gap where the stock postprocessor does the wrong thing for a
desktop user, and reuses FFmpegPostProcessor for binary discovery, argument
handling and error reporting:

* ForceRecodePP: FFmpegVideoConvertor skips a file already in the target
  container, so "H.264 in MP4" did nothing to AV1-in-MP4, exactly when the
  re-encode was wanted. This one always encodes when asked to.
* NormalizeAudioPP: loudness levelling for video downloads that are not being
  re-encoded, touching only the sound.
* NormalizedExtractAudioPP: FFmpegExtractAudio copies the stream when the codec
  already matches, and ffmpeg cannot filter a copied stream. This variant always
  encodes, so the loudness filter applies in the same encode.

Loudness is levelled in two passes: a quick scan measures the sound, then the
encode applies loudnorm with those measurements. One pass guesses from the
first seconds and missed -14 LUFS by 2 to 3 LU on short clips.
* SafeEmbedThumbnailPP: EmbedThumbnail raises for WAV, WebM and (without
  mutagen, which the installer leaves out) Opus/FLAC/OGG, failing a finished
  download over cover art. This one skips those and never fails the job.
* RequestNamePP: yt-dlp names a clip, a 480p copy and a full 4K download of
  one video the same, then hands back whichever is already on disk. This one
  runs after the format is chosen and gives the file a name that says what it
  is when it would otherwise land on a different existing file.

The encoding ones can be stopped: a re-encode of a long video takes many
minutes, and yt-dlp's own ffmpeg runner cannot be interrupted, so Cancel would
otherwise keep a worker busy until the encode finished.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import threading

from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP
from yt_dlp.postprocessor.ffmpeg import (FFmpegConcatPP, FFmpegExtractAudioPP,
                                         FFmpegPostProcessor, FFmpegPostProcessorError)
from yt_dlp.utils import (DownloadCancelled, Popen, PostProcessingError, encodeArgument,
                          orderedSet, prepend_extension, replace_extension)

from . import ffmpegtools


def embeddable_exts() -> set[str]:
    """Containers yt-dlp can put cover art into in this build."""
    exts = {"mp3", "mkv", "mka", "mp4", "m4a", "m4v", "mov"}
    try:
        from yt_dlp.dependencies import mutagen
    except Exception:
        mutagen = None
    if mutagen:
        exts |= {"ogg", "opus", "flac"}
    return exts


def _reused(info: dict) -> bool:
    """The media file was already on disk from an earlier identical download, so
    it has been through this chain once; doing it again would only lose quality."""
    return bool(info.get("__mt_reused"))


class _Stoppable:
    """Run ffmpeg so that a cancel request kills it within half a second.

    Mirrors FFmpegPostProcessor.real_run_ffmpeg (same flags, 'file:' paths,
    +faststart) but keeps the process handle. The app never sets per-PP user
    arguments, so those are not looked up here.
    """
    _should_stop = None          # callable that raises when the job is cancelled

    def real_run_ffmpeg(self, input_path_opts, output_path_opts, *, expected_retcodes=(0,)):
        self.check_version()
        cmd = [self.executable, "-y", "-loglevel", "repeat+info"]
        for path, opts in input_path_opts:
            if path:
                cmd += [encodeArgument(a) for a in opts] + ["-i", self._ffmpeg_filename_argument(path)]
        for path, opts in output_path_opts:
            if path:
                cmd += [encodeArgument(a) for a in itertools.chain(opts, ["-movflags", "+faststart"])]
                cmd.append(self._ffmpeg_filename_argument(path))
        self.write_debug(f"ffmpeg command line: {cmd}")

        try:
            oldest_mtime = min(os.stat(path).st_mtime for path, _ in input_path_opts if path)
        except (OSError, ValueError):
            oldest_mtime = None
        returncode, stderr = self._run_stoppable(
            cmd, outputs=[path for path, _ in output_path_opts if path])
        if returncode not in (expected_retcodes if isinstance(expected_retcodes, (list, tuple))
                              else (expected_retcodes,)):
            self.write_debug(stderr)
            lines = stderr.strip().splitlines()
            raise FFmpegPostProcessorError(lines[-1] if lines else f"ffmpeg exited with code {returncode}")
        if oldest_mtime is not None:
            for path, _ in output_path_opts:
                if path:
                    self.try_utime(path, oldest_mtime, oldest_mtime)
        return stderr

    def _run_stoppable(self, cmd: list[str], outputs=()) -> tuple[int, str]:
        """Run ffmpeg, checking for Cancel every half second. On Cancel it is
        killed and its unfinished outputs are removed. Returns (exit code,
        the last lines of its log)."""
        # yt-dlp's Popen decodes as UTF-8 with replacement, so a non-ASCII
        # file name in ffmpeg's log can never kill the reader thread (and a
        # dead reader would leave ffmpeg blocked on a full pipe).
        proc = Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.PIPE, text=True)
        tail: list[str] = []

        def drain():
            for line in proc.stderr:
                tail.append(line)
                del tail[:-60]
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if self._should_stop:
                        self._should_stop()
        except BaseException:
            proc.kill()
            proc.wait()
            for path in outputs:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            raise
        reader.join(timeout=2)
        return proc.returncode, "".join(tail)

    def measure_loudness(self, path: str) -> dict | None:
        """First of two loudness passes: how loud the sound is now. None when
        it cannot be told; the filter then works in one pass."""
        cmd = [self.executable, "-hide_banner", "-nostdin", "-nostats",
               "-i", self._ffmpeg_filename_argument(path), *ffmpegtools.loudness_scan_args(), "-"]
        self.write_debug(f"ffmpeg command line: {cmd}")
        try:
            returncode, stderr = self._run_stoppable(cmd)
        except DownloadCancelled:
            raise
        except Exception:
            return None
        return ffmpegtools.parse_loudness(stderr) if returncode == 0 else None


class ForceRecodePP(_Stoppable, FFmpegPostProcessor):
    def __init__(self, downloader=None, encoder: str = "", quality: str = "balanced",
                 container: str = "mp4", normalize: bool = False, should_stop=None):
        super().__init__(downloader)
        self._encoder = encoder
        self._quality = quality
        self._ext = ffmpegtools.recode_container(encoder, container)
        self._normalize = bool(normalize)
        self._should_stop = should_stop

    def run(self, info):
        path = info.get("filepath")
        if not path or not os.path.exists(path) or _reused(info):
            return [], info
        facts = ffmpegtools.probe(path)
        if facts and not facts.get("vcodec"):
            # Sound only (a podcast, or an audio stream picked by hand): there
            # is no picture to re-encode, and "-map 0:v:0" would fail the job.
            self.to_screen(f"No picture in {os.path.basename(path)}; keeping it as it is")
            return [], info
        try:
            source_audio = self.get_audio_codec(path)
        except Exception:
            source_audio = None
        args = ["-map", "0:v:0", "-map", "0:a?", "-dn"]
        args += ffmpegtools.video_args(self._encoder, self._quality, self._ext)
        if source_audio:
            loudness = self.measure_loudness(path) if self._normalize else None
            args += ffmpegtools.audio_args(self._ext, source_audio, self._normalize, self._quality,
                                           loudness=loudness)

        target = f"{os.path.splitext(path)[0]}.{self._ext}"
        temp = prepend_extension(target, "recode")
        self.to_screen(f"Re-encoding {os.path.basename(path)} with {self._encoder}")
        try:
            self.run_ffmpeg(path, temp, args)
        except DownloadCancelled:
            raise
        except Exception as exc:
            if os.path.exists(temp):
                os.remove(temp)
            raise PostProcessingError(f"Re-encode failed: {exc}") from exc

        os.replace(temp, target)
        if os.path.normcase(target) != os.path.normcase(path) and os.path.exists(path):
            os.remove(path)
        info["filepath"] = target
        info["ext"] = self._ext
        return [], info


class NormalizeAudioPP(_Stoppable, FFmpegPostProcessor):
    """Even out the volume of a video file without touching the picture."""

    def __init__(self, downloader=None, quality: str = "high", should_stop=None):
        super().__init__(downloader)
        self._quality = quality
        self._should_stop = should_stop

    def run(self, info):
        path = info.get("filepath")
        if not path or not os.path.exists(path) or _reused(info):
            return [], info
        source_audio = self.get_audio_codec(path)
        if not source_audio:
            self.to_screen("No sound to level")
            return [], info
        ext = (info.get("ext") or os.path.splitext(path)[1][1:]).lower()
        self.to_screen(f"Levelling the volume of {os.path.basename(path)}")
        loudness = self.measure_loudness(path)
        args = ["-map", "0", "-dn", "-ignore_unknown", "-c", "copy"]
        args += ffmpegtools.audio_args(ext, source_audio, True, self._quality, loudness=loudness)
        temp = prepend_extension(path, "temp")
        try:
            self.run_ffmpeg(path, temp, args)
        except DownloadCancelled:
            raise
        except Exception as exc:
            if os.path.exists(temp):
                os.remove(temp)
            raise PostProcessingError(f"Volume levelling failed: {exc}") from exc
        os.replace(temp, path)
        return [], info


class NormalizedExtractAudioPP(_Stoppable, FFmpegExtractAudioPP):
    """Extract audio and level its loudness in one encode."""

    def __init__(self, downloader=None, preferredcodec=None, preferredquality=None,
                 nopostoverwrites=False, should_stop=None):
        super().__init__(downloader, preferredcodec, preferredquality, nopostoverwrites)
        self._should_stop = should_stop

    def get_audio_codec(self, path):
        real = super().get_audio_codec(path)
        # Never equal to the target codec, so the parent never picks "copy".
        return None if real is None else "__normalize__"

    def run(self, information):
        if _reused(information):
            return [], information
        return super().run(information)

    def run_ffmpeg(self, path, out_path, codec, more_opts):
        extra = list(more_opts)
        if codec == "libopus" and "-b:a" not in extra:
            extra += ["-b:a", "160k"]     # libopus defaults to 96k, thin for music
        loudness = self.measure_loudness(path)
        super().run_ffmpeg(path, out_path, codec, extra + ffmpegtools.loudnorm_args(loudness))


class SafeEmbedThumbnailPP(EmbedThumbnailPP):
    """Cover art where the container supports it; a note (never a failure) elsewhere."""

    def __init__(self, downloader=None, already_have_thumbnail=False, on_note=None):
        super().__init__(downloader, already_have_thumbnail=already_have_thumbnail)
        self._on_note = on_note

    def _note(self, text: str) -> None:
        if self._on_note:
            try:
                self._on_note(text)
            except Exception:
                pass

    def _thumbnail_files(self, info) -> list[str]:
        if self._already_have_thumbnail:
            return []
        return [t["filepath"] for t in (info.get("thumbnails") or [])
                if t.get("filepath") and os.path.exists(t["filepath"])]

    def run(self, info):
        ext = (info.get("ext") or "").lower()
        if ext not in embeddable_exts():
            self.to_screen(f"Cover art cannot go into a .{ext} file; skipping it")
            self._note(f"Cover art isn't supported in {ext.upper()} files, so it was left out.")
            return self._thumbnail_files(info), info
        try:
            return super().run(info)
        except DownloadCancelled:
            raise          # the user pressed Cancel; that is not a cover-art problem
        except Exception as exc:
            self.report_warning(f"Could not add cover art: {exc}")
            self._note("The cover art couldn't be added. The file itself is fine.")
            return self._thumbnail_files(info), info


class KeepOldConcatPP(_Stoppable, FFmpegConcatPP):
    """"Join the playlist into one file", without deleting anything that was
    already on disk before this run: yt-dlp deletes every joined item, which
    includes videos a user downloaded last week and that were only reused.
    With a single item yt-dlp renames it instead of joining; that is skipped
    too, so an older file is never moved away under a new name."""

    def __init__(self, downloader=None, only_multi_video=False, should_stop=None):
        super().__init__(downloader, only_multi_video)
        self._should_stop = should_stop

    @classmethod
    def pp_key(cls):
        return "Concat"          # progress text and per-PP arguments follow yt-dlp's key

    def concat_files(self, in_files, out_file):
        if len(in_files) < 2:
            return []            # nothing to join; the item stays where it is
        return super().concat_files(in_files, out_file)

    def run(self, info):
        keep = set()
        for entry in info.get("entries") or []:
            for got in (entry or {}).get("requested_downloads") or []:
                if got.get("__mt_reused") and got.get("filepath"):
                    keep.add(os.path.normcase(os.path.abspath(got["filepath"])))
        to_delete, info = super().run(info)
        return [p for p in (to_delete or [])
                if os.path.normcase(os.path.abspath(p)) not in keep], info


# ---------------------------------------------------------------- file names

_FAMILIES = (("avc", "h264"), ("h264", "h264"), ("hev", "hevc"), ("hvc", "hevc"),
             ("h265", "hevc"), ("hevc", "hevc"), ("av01", "av1"), ("av1", "av1"),
             ("vp09", "vp9"), ("vp9", "vp9"), ("vp8", "vp8"))
_FAMILY_LABEL = {"h264": "H.264", "hevc": "H.265", "av1": "AV1", "vp9": "VP9", "vp8": "VP8"}


def codec_family(name: str | None) -> str:
    name = (name or "").lower()
    if name in ("", "none"):
        return ""
    return next((fam for prefix, fam in _FAMILIES if name.startswith(prefix)), name.split(".")[0])


def clock(seconds: float) -> str:
    """0:10 as '0.10', 1:02:03 as '1.02.03': a time Windows accepts in a name."""
    s = int(seconds or 0)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}.{m:02d}.{sec:02d}" if h else f"{m}.{sec:02d}"


def section_label(info: dict) -> str:
    """' (Intro)' or ' (0.10-0.14)' for a clip, '' for the whole video."""
    title = str(info.get("section_title") or "").strip()
    if title:
        return f" ({title[:40]})"      # the name budget leaves room for this much
    start, end = info.get("section_start"), info.get("section_end")
    if start is None and not end:
        return ""
    return f" ({clock(start or 0)}-{clock(end) if end else 'end'})"


class RequestNamePP(PostProcessor):
    """Runs at the 'video' stage: the format is chosen, the name is not made yet.

    Tells the app a download is starting (``on_video``), marks clips in the
    name, and checks the file yt-dlp would treat as "already downloaded".
    When that file is really the same thing (same picture height, codec and
    length) it is reused and flagged so the re-encode passes leave it alone.
    When it is something else (a clip, another quality, the video when sound
    was asked for) the new file gets a name of its own instead of the old
    file being returned, or, for audio, deleted after extracting from it.
    """

    def __init__(self, downloader=None, on_video=None, recode_codec: str = "",
                 audio_only: bool = False, cuts: bool = False):
        super().__init__(downloader)
        self._on_video = on_video
        self._recode = recode_codec
        self._audio_only = audio_only
        # Chapters or sponsor segments are cut out afterwards, so the saved
        # file is shorter than the video and its length says nothing.
        self._cuts = cuts

    @classmethod
    def pp_key(cls):
        return "RequestName"

    def _expected(self, info: dict) -> dict:
        has_video = not self._audio_only and info.get("vcodec") not in (None, "none")
        start, end = info.get("section_start"), info.get("section_end")
        duration = info.get("duration")
        if start is not None or end:
            total = duration or 0
            duration = ((end or total) - (start or 0)) if (end or total) else None
        width, height = info.get("width"), info.get("height")
        return {"video": has_video,
                "height": height if has_video else None,
                # The name tag says "1080p" for a 1080x1920 vertical video too.
                "res": (min(width, height) if width and height else height) if has_video else None,
                "codec": (self._recode or codec_family(info.get("vcodec"))) if has_video else "",
                "duration": None if self._cuts else duration}

    @staticmethod
    def _found(path: str) -> dict:
        facts = ffmpegtools.probe(path)
        if not facts:
            return {}
        return {"video": bool(facts.get("height") or facts.get("vcodec")),
                "height": facts.get("height") or None,
                "codec": codec_family(facts.get("vcodec")),
                "duration": facts.get("duration") or None}

    @staticmethod
    def _same(want: dict, got: dict) -> bool:
        if not got:
            return True            # unreadable: keep yt-dlp's own behaviour
        if want["video"] != got["video"]:
            return False
        if want["height"] and got["height"] and want["height"] != got["height"]:
            return False
        if want["codec"] and got["codec"] and want["codec"] != got["codec"]:
            return False
        a, b = want["duration"], got["duration"]
        if a and b and abs(a - b) > max(2.0, 0.03 * a):
            return False
        return True

    @staticmethod
    def _tag(want: dict, got: dict) -> str:
        if not want["video"]:
            return "audio"
        res = want.get("res") or want["height"]
        parts = [f"{res}p"] if res else []
        if want["codec"] and want["codec"] != got.get("codec"):
            parts.append(_FAMILY_LABEL.get(want["codec"], want["codec"].upper()))
        if not parts or (want["height"] == got.get("height") and want["codec"] == got.get("codec")):
            a, b = want["duration"] or 0, got.get("duration") or 0
            parts.append("full" if a > b else "clip")
        return " ".join(parts)

    def _existing(self, info: dict) -> str | None:
        ydl = self._downloader
        full = ydl.prepare_filename(info)
        if not full:
            return None
        ext = info.get("ext") or ""
        final = ydl.params.get("final_ext") or ext
        # The same candidates yt-dlp's own "already downloaded" check uses.
        for path in orderedSet([replace_extension(full, final, ext), full]):
            if os.path.exists(path):
                return path
        return None

    def _name(self, info: dict) -> None:
        info.pop("__mt_reused", None)
        base = section_label(info)
        info["mt_suffix"] = base
        existing = self._existing(info)
        if not existing:
            return
        want = self._expected(info)
        got = self._found(existing)
        if self._same(want, got):
            info["__mt_reused"] = True
            return
        tag = self._tag(want, got)
        for n in range(1, 20):
            info["mt_suffix"] = f"{base} ({tag})" if n == 1 else f"{base} ({tag} {n})"
            existing = self._existing(info)
            if not existing:
                break
            if self._same(want, self._found(existing)):
                info["__mt_reused"] = True
                break
        self.to_screen(f"Saving as a new file: another version already has this name ({tag})")

    def run(self, info):
        self._name(info)
        if self._on_video:
            try:
                planned = self._downloader.prepare_filename(info)
            except Exception:
                planned = ""
            self._on_video(info, planned)
        return [], info


def skip_when_reused(pp):
    """Make one postprocessor leave a reused file alone.

    Used for the steps that must not run twice on the same file: cutting
    chapters out again fails ("durations mismatch"), and writing or splitting
    by the site's chapter list would use times from before the cut.
    """
    run = pp.run

    def guarded(info, *args, **kwargs):
        if info.get("__mt_reused"):
            return [], info
        return run(info, *args, **kwargs)
    pp.run = guarded
    return pp
