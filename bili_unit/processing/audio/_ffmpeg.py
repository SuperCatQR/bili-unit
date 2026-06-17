# audio/_ffmpeg — ffmpeg binary discovery with imageio-ffmpeg fallback.
#
# BILI_PROCESSING_FFMPEG_PATH defaults to "auto", which resolves in this order:
#   1. system ffmpeg via shutil.which("ffmpeg")
#   2. imageio-ffmpeg's bundled binary (if installed)
#   3. raise FFmpegUnavailable
#
# Setting BILI_PROCESSING_FFMPEG_PATH to an explicit path bypasses discovery.
# Setting it to "system" forces shutil.which("ffmpeg") with no fallback.
# Setting it to "imageio" forces the bundled binary with no fallback.

from __future__ import annotations

import logging
import shutil
from functools import lru_cache

logger = logging.getLogger("bili.processing.audio.ffmpeg")


class FFmpegUnavailable(RuntimeError):
    """Neither system ffmpeg nor imageio-ffmpeg is available."""


def _try_system() -> str | None:
    return shutil.which("ffmpeg")


def _try_imageio() -> str | None:
    try:
        import imageio_ffmpeg as iio  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return iio.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 — defensive: bundled binary may be missing
        logger.warning("imageio_ffmpeg.get_ffmpeg_exe failed: %s", exc)
        return None


@lru_cache(maxsize=8)
def resolve_ffmpeg(setting: str = "auto") -> str:
    """Return the absolute path to an ffmpeg binary.

    Args:
        setting: BILI_PROCESSING_FFMPEG_PATH value. Special values:
            "auto"     try system, then imageio-ffmpeg
            "system"   only system ffmpeg
            "imageio"  only imageio-ffmpeg bundled binary
            anything else is treated as an explicit path.

    Raises:
        FFmpegUnavailable: when no usable binary is found.
    """
    s = (setting or "auto").strip()
    low = s.lower()

    if low == "system":
        path = _try_system()
        if path:
            return path
        raise FFmpegUnavailable("system ffmpeg not found in PATH")

    if low == "imageio":
        path = _try_imageio()
        if path:
            return path
        raise FFmpegUnavailable(
            "imageio-ffmpeg not installed",
        )

    if low == "auto":
        path = _try_system()
        if path:
            logger.debug("ffmpeg resolved via system PATH: %s", path)
            return path
        path = _try_imageio()
        if path:
            logger.debug("ffmpeg resolved via imageio-ffmpeg: %s", path)
            return path
        raise FFmpegUnavailable(
            "no ffmpeg available; install system ffmpeg or imageio-ffmpeg",
        )

    # explicit path
    if shutil.which(s):
        return s
    # treat as a literal path even if not on PATH (caller may have absolute path)
    return s


def is_available(setting: str = "auto") -> bool:
    """Return True iff ``resolve_ffmpeg(setting)`` would succeed."""
    try:
        resolve_ffmpeg(setting)
        return True
    except FFmpegUnavailable:
        return False
