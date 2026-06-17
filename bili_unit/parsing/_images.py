# _images — ImageDownloader for the parsing layer.
#
# Downloads images concurrently via aiohttp with:
#   - Semaphore-based concurrency control
#   - Referer + User-Agent headers (matches AudioDownloader pattern)
#   - Extension inference from URL path, Content-Type fallback
#   - Failure isolation (single image failure doesn't block others)

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import aiohttp

logger = logging.getLogger("bili.parsing.images")

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_DEFAULT_REFERER = "https://www.bilibili.com"

# Content-Type → file extension mapping
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}


@dataclass
class ImageDownloadResult:
    url: str
    local_path: str           # logical path used by parsed payloads
    status: str               # "ok" | "skipped" | "failed"
    error: str = ""
    data: bytes | None = None


class ImageDownloader:
    """Concurrent image downloader that returns image bytes for DB storage."""

    def __init__(
        self,
        base_dir: Path,
        concurrency: int = 8,
        timeout: float = 30.0,
    ) -> None:
        self._base_dir = base_dir
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def download_one(
        self,
        url: str,
        dest_rel: str,
    ) -> ImageDownloadResult:
        """Download a single image.

        Args:
            url: Remote image URL.
            dest_rel: Relative path within the images/ directory.

        Returns:
            ImageDownloadResult with status "ok", "skipped", or "failed".
        """
        async with self._semaphore:
            try:
                headers = {
                    "Referer": _DEFAULT_REFERER,
                    "User-Agent": _DEFAULT_UA,
                }
                async with (
                    aiohttp.ClientSession(timeout=self._timeout) as session,
                    session.get(url, headers=headers) as resp,
                ):
                    if resp.status != 200:
                        return ImageDownloadResult(
                            url=url, local_path=dest_rel,
                            status="failed",
                            error=f"HTTP {resp.status}",
                        )

                    data = await resp.read()

                    # Possibly update extension from Content-Type
                    content_type = resp.headers.get("Content-Type", "")
                    final_rel = self._maybe_fix_extension(
                        dest_rel, content_type,
                    )

                return ImageDownloadResult(
                    url=url, local_path=final_rel, status="ok", data=data,
                )

            except TimeoutError:
                return ImageDownloadResult(
                    url=url, local_path=dest_rel,
                    status="failed", error="timeout",
                )
            except aiohttp.ClientError as exc:
                return ImageDownloadResult(
                    url=url, local_path=dest_rel,
                    status="failed", error=str(exc),
                )
            except Exception as exc:
                return ImageDownloadResult(
                    url=url, local_path=dest_rel,
                    status="failed", error=f"unexpected: {exc}",
                )

    async def download_many(
        self,
        jobs: list[tuple[str, str]],
    ) -> list[ImageDownloadResult]:
        """Download multiple images concurrently.

        Args:
            jobs: List of (url, dest_rel) tuples.

        Returns:
            List of ImageDownloadResult in the same order as jobs.
        """
        tasks = [
            self.download_one(url, dest_rel)
            for url, dest_rel in jobs
        ]
        return await asyncio.gather(*tasks)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _maybe_fix_extension(
        dest_rel: str, content_type: str,
    ) -> str:
        """If the logical path has no extension or a generic one, use Content-Type."""
        dest_path = Path(dest_rel)
        if not content_type:
            return dest_rel

        # Extract base type (ignore charset etc.)
        mime = content_type.split(";")[0].strip().lower()
        ext = _CONTENT_TYPE_EXT.get(mime)
        if ext is None:
            return dest_rel

        # If the current suffix matches a known image extension, keep it
        current_suffix = dest_path.suffix.lower()
        known_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}
        if current_suffix in known_suffixes:
            return dest_rel

        # Replace or append extension
        return dest_path.with_suffix(ext).as_posix()


def infer_extension_from_url(url: str) -> str:
    """Infer file extension from URL path.

    B站 CDN URLs typically end with .jpg, .png, .webp etc.
    Returns "" if no recognizable extension is found.
    """
    from urllib.parse import urlparse

    try:
        path = urlparse(url).path
    except Exception:
        return ""

    suffix = Path(path).suffix.lower()
    known = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}
    if suffix in known:
        return suffix
    return ".jpg"  # default for B站 CDN
