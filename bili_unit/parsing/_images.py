# _images — ImageDownloader for the parsing layer.
#
# Downloads images concurrently via aiohttp with:
#   - Semaphore-based concurrency control
#   - Skip already-downloaded files (size > 0)
#   - Referer + User-Agent headers (matches AudioDownloader pattern)
#   - Extension inference from URL path, Content-Type fallback
#   - Failure isolation (single image failure doesn't block others)
#   - File writes via asyncio.to_thread to avoid blocking the event loop

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
    local_path: str           # relative to images/ directory
    status: str               # "ok" | "skipped" | "failed"
    error: str = ""


class ImageDownloader:
    """Concurrent image downloader with dedup and skip-existing."""

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
        dest_path = self._base_dir / dest_rel

        # Skip if file already exists and has content
        if await asyncio.to_thread(self._file_exists_nonempty, dest_path):
            return ImageDownloadResult(
                url=url, local_path=dest_rel, status="skipped",
            )

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

                    # Possibly update extension from Content-Type
                    content_type = resp.headers.get("Content-Type", "")
                    final_path = self._maybe_fix_extension(
                        dest_path, content_type,
                    )
                    final_rel = str(final_path.relative_to(self._base_dir))

                    data = await resp.read()

                # Write file in thread to avoid blocking
                await asyncio.to_thread(self._write_file, final_path, data)

                return ImageDownloadResult(
                    url=url, local_path=final_rel, status="ok",
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
    def _file_exists_nonempty(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _maybe_fix_extension(
        dest_path: Path, content_type: str,
    ) -> Path:
        """If the dest_path has no extension or a generic one, use Content-Type."""
        if not content_type:
            return dest_path

        # Extract base type (ignore charset etc.)
        mime = content_type.split(";")[0].strip().lower()
        ext = _CONTENT_TYPE_EXT.get(mime)
        if ext is None:
            return dest_path

        # If the current suffix matches a known image extension, keep it
        current_suffix = dest_path.suffix.lower()
        known_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}
        if current_suffix in known_suffixes:
            return dest_path

        # Replace or append extension
        return dest_path.with_suffix(ext)


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
