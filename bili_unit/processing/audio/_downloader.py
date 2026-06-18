# audio/_downloader — bilibili CDN audio stream downloader.
#
#   1. Video(bvid, credential=cred).get_download_url_data()
#   2. VideoDownloadURLDataDetecter(data).detect(audio_max_quality=...)
#   3. Filter by type(stream).__name__ == "AudioStreamDownloadURL"
#      (workaround for bilibili-api 17.x detect_best_streams NoneType bug)
#   4. Download audio stream via aiohttp with Referer + User-Agent headers.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
from bilibili_api.video import AudioQuality, Video, VideoDownloadURLDataDetecter

from .. import DownloadError

if TYPE_CHECKING:
    from bilibili_api import Credential

logger = logging.getLogger("bili.processing.audio.downloader")

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_DEFAULT_REFERER = "https://www.bilibili.com"


class AudioDownloader:
    """Download audio streams from bilibili CDN.

    Wraps ``bilibili-api-python`` for URL resolution and ``aiohttp`` for
    the actual byte download.  The bilibili-api 17.x ``detect_best_streams``
    bug is worked around by using ``detecter.detect()`` with an explicit
    quality parameter and filtering by class name.
    """

    def __init__(
        self,
        credential: Credential | None = None,
        *,
        download_timeout_s: int = 600,
        max_size_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self._credential = credential
        self._download_timeout_s = download_timeout_s
        self._max_size_bytes = max_size_bytes

    async def get_audio_url(
        self,
        bvid: str,
        page_index: int = 0,
        quality: str = "64K",
    ) -> dict:
        """Resolve the CDN audio URL for a video page.

        Returns a dict with ``url``, ``quality``, and ``duration`` (seconds).

        Raises:
            DownloadError: when no audio stream is found or the API fails.
        """
        video = Video(bvid=bvid, credential=self._credential)
        try:
            data = await video.get_download_url(page_index=page_index)
        except Exception as exc:
            raise DownloadError(
                f"get_download_url failed for {bvid}: {exc}"
            ) from exc

        detecter = VideoDownloadURLDataDetecter(data)
        quality_enum = _resolve_quality(quality)
        streams = detecter.detect(audio_max_quality=quality_enum)

        audio_stream = None
        for s in streams:
            if type(s).__name__ == "AudioStreamDownloadURL":
                audio_stream = s
                break

        if audio_stream is None:
            # Fallback: accept any audio stream regardless of quality.
            for s in streams:
                if type(s).__name__ == "AudioStreamDownloadURL":
                    audio_stream = s
                    break
            if audio_stream is None:
                raise DownloadError(
                    f"no audio stream found for {bvid} page {page_index}"
                )

        # Extract duration from the raw data when available.
        duration = _extract_duration(data)

        return {
            "url": audio_stream.url,
            "quality": str(getattr(audio_stream, "audio_quality", quality)),
            "duration": duration,
        }

    async def download_to_file(
        self,
        url: str,
        dest_path: str,
        bvid: str = "",
    ) -> None:
        """Download audio bytes from *url* and write to *dest_path*.

        Raises:
            DownloadError: on network, timeout, I/O failure, or size exceeded.
        """
        headers = {
            "Referer": f"{_DEFAULT_REFERER}/video/{bvid}" if bvid else _DEFAULT_REFERER,
            "User-Agent": _DEFAULT_UA,
        }
        timeout = aiohttp.ClientTimeout(
            total=self._download_timeout_s,
            sock_read=60,
        )
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, headers=headers) as resp,
            ):
                if resp.status != 200:
                    raise DownloadError(
                        f"CDN download returned {resp.status} for {url}"
                    )
                total_bytes = 0
                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        total_bytes += len(chunk)
                        if total_bytes > self._max_size_bytes:
                            raise DownloadError(
                                f"CDN download exceeded {self._max_size_bytes} bytes for {url}"
                            )
                        f.write(chunk)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DownloadError(f"CDN download failed for {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_quality(quality: str):
    """Map a quality string like ``"64K"`` to ``AudioQuality._64K``."""
    mapping = {
        "64K": AudioQuality._64K,
        "132K": AudioQuality._132K,
        "192K": getattr(AudioQuality, "_192K", AudioQuality._132K),
        "dolby": getattr(AudioQuality, "DOLBY", AudioQuality._132K),
        "hires": getattr(AudioQuality, "HI_RES", AudioQuality._132K),
    }
    return mapping.get(quality.upper().replace(" ", ""), AudioQuality._64K)


def _extract_duration(data: dict) -> float | None:
    """Try to pull a duration value from the raw download-URL response."""
    # bilibili-api nests duration in various places depending on API version.
    for key in ("dash", "data"):
        inner = data.get(key)
        if isinstance(inner, dict):
            dur = inner.get("duration")
            if dur is not None:
                return float(dur) / 1000.0  # ms → seconds
    dur = data.get("duration")
    if dur is not None:
        return float(dur)
    return None
