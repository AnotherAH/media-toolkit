"""A postprocessor that always re-encodes.

yt-dlp's FFmpegVideoConvertor deliberately skips a file that is already in the
target container -- so asking for "H.264 in MP4" does nothing when YouTube
already handed us AV1 inside MP4, which is exactly when you wanted the re-encode.
This subclass keys off the codec instead of the extension, and reuses
FFmpegPostProcessor for binary discovery and argument handling.
"""
from __future__ import annotations

import os

from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import PostProcessingError, prepend_extension


class ForceRecodePP(FFmpegPostProcessor):
    def __init__(self, downloader=None, args: list[str] | None = None,
                 ext: str = "mp4", target_vcodec: str = ""):
        super().__init__(downloader)
        self._args = list(args or [])
        self._ext = ext.lstrip(".")
        # Family name, e.g. "h264" from "h264_nvenc", used to skip a no-op pass.
        self._target = (target_vcodec or "").split("_")[0]

    def _already_target(self, info: dict) -> bool:
        if not self._target:
            return False
        current = (info.get("vcodec") or "").lower()
        aliases = {"h264": ("avc1", "h264", "avc"), "hevc": ("hev1", "hvc1", "h265", "hevc"),
                   "av1": ("av01", "av1")}
        names = aliases.get(self._target, (self._target,))
        return (current.startswith(names) and info.get("ext") == self._ext)

    def run(self, info):
        path = info.get("filepath")
        if not path or not os.path.exists(path):
            return [], info
        if self._already_target(info):
            self.to_screen(f"Skipping re-encode; already {self._target} in {self._ext}")
            return [], info

        target = f"{os.path.splitext(path)[0]}.{self._ext}"
        temp = prepend_extension(target, "recode")
        self.to_screen(f"Re-encoding {os.path.basename(path)} to {self._target or self._ext}")
        try:
            self.run_ffmpeg(path, temp, self._args)
        except Exception as exc:
            for leftover in (temp,):
                if os.path.exists(leftover):
                    os.remove(leftover)
            raise PostProcessingError(f"Re-encode failed: {exc}") from exc

        os.replace(temp, target)
        if os.path.normcase(target) != os.path.normcase(path) and os.path.exists(path):
            os.remove(path)
        info["filepath"] = target
        info["ext"] = self._ext
        return [], info
