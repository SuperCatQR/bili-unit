# audio/_asr_cache — per-segment ASR result cache (resume-from-failure).
#
# Why this exists:
#   ASR is the single most expensive + failure-prone step in the audio
#   pipeline.  A bvid that splits into N segments costs N MiMo API calls;
#   a network blip or quota exhaustion on segment 4 of 6 currently invalidates
#   the entire page and re-bills the first 3 segments on retry.
#
#   This module caches each successful ASR call keyed by ``(start_s, end_s)``
#   on the original audio timeline.  Subsequent runs that produce the same
#   cut plan (VAD is deterministic for the same input + threshold) skip the
#   API call and reuse the cached text.
#
# Storage layout (one JSON per page, atomic-rename on write):
#   {root}/{uid}/{bvid}/{page_index}.json
#       {
#         "version": 1,
#         "uid": <int>, "bvid": "<str>", "page_index": <int>,
#         "segments": [
#           {"start_s": <float>, "end_s": <float>, "text": <str>,
#            "language": <str>, "duration": <float|null>, "model": <str>},
#           ...
#         ],
#         "updated_at": <ms>
#       }
#
# Match policy: a cache entry hits when both endpoints are within
# ``MATCH_TOLERANCE_S`` (0.1 s) of the queried range.  ffmpeg's ``-ss``/``-to``
# seek accuracy at our settings is ~0.06 s; 0.1 s tolerates that without
# letting genuinely different cuts collide.

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bili.processing.audio.asr_cache")

CACHE_VERSION = 1
MATCH_TOLERANCE_S = 0.1


@dataclass(frozen=True)
class CachedSegment:
    """One ASR result cached on its source ``(start_s, end_s)`` timeline."""

    start_s: float
    end_s: float
    text: str
    language: str
    duration: float | None
    model: str

    def to_dict(self) -> dict:
        return {
            "start_s": self.start_s,
            "end_s": self.end_s,
            "text": self.text,
            "language": self.language,
            "duration": self.duration,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> CachedSegment:
        return cls(
            start_s=float(raw["start_s"]),
            end_s=float(raw["end_s"]),
            text=str(raw.get("text", "")),
            language=str(raw.get("language", "")),
            duration=(
                float(raw["duration"]) if raw.get("duration") is not None else None
            ),
            model=str(raw.get("model", "")),
        )


@dataclass
class _PageCache:
    """In-memory mirror of a single page's cache file."""

    uid: int
    bvid: str
    page_index: int
    segments: list[CachedSegment] = field(default_factory=list)


class ASRCacheStore:
    """Per-segment ASR cache rooted at *root_dir*.

    Thread-safe: writes are guarded by a single ``threading.Lock``.  Designed
    for synchronous callers — the bili audio pipeline calls these from inside
    ``_do_audio_work`` which is already inside an asyncio task; the I/O is
    small (one tiny JSON per page) so we don't run them in a thread pool.

    The lock protects the *file* layer; if two workers process the same
    (uid, bvid) at once they race on read-modify-write of the same JSON.
    The runner serialises bvids per-worker, so contention is incidental
    rather than the steady state.
    """

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._lock = threading.Lock()

    # -- path helpers ------------------------------------------------------

    def _page_path(self, uid: int, bvid: str, page_index: int) -> Path:
        return self._root / str(uid) / bvid / f"{page_index}.json"

    def _bvid_dir(self, uid: int, bvid: str) -> Path:
        return self._root / str(uid) / bvid

    # -- I/O ---------------------------------------------------------------

    def load_page(self, uid: int, bvid: str, page_index: int) -> _PageCache:
        """Load cache for one (uid, bvid, page); empty cache if missing."""
        path = self._page_path(uid, bvid, page_index)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _PageCache(uid=uid, bvid=bvid, page_index=page_index)
        except json.JSONDecodeError as exc:
            # Treat corrupt cache as a cold start; warn loudly so it surfaces.
            logger.warning(
                "asr_cache_corrupt_dropped",
                extra={"path": str(path), "error": str(exc)},
            )
            return _PageCache(uid=uid, bvid=bvid, page_index=page_index)

        version = int(raw.get("version", 0))
        if version != CACHE_VERSION:
            # Schema upgrade path — drop on mismatch rather than half-honour.
            logger.info(
                "asr_cache_version_mismatch_dropped",
                extra={"path": str(path), "got": version, "want": CACHE_VERSION},
            )
            return _PageCache(uid=uid, bvid=bvid, page_index=page_index)

        segs_raw = raw.get("segments") or []
        segments = [CachedSegment.from_dict(s) for s in segs_raw]
        return _PageCache(
            uid=uid, bvid=bvid, page_index=page_index, segments=segments,
        )

    def find(
        self,
        page: _PageCache,
        start_s: float,
        end_s: float,
    ) -> CachedSegment | None:
        """Return a cached segment matching ``(start_s, end_s)`` or None."""
        for s in page.segments:
            if (
                abs(s.start_s - start_s) <= MATCH_TOLERANCE_S
                and abs(s.end_s - end_s) <= MATCH_TOLERANCE_S
            ):
                return s
        return None

    def upsert(self, page: _PageCache, seg: CachedSegment) -> None:
        """Insert or replace *seg* in *page*; persist immediately.

        Replace key is the same tolerance match as :meth:`find`.  We always
        flush after each upsert: the whole point of the cache is to survive
        hard interruptions, so a buffered write defeats the purpose.
        """
        replaced = False
        new_segments: list[CachedSegment] = []
        for existing in page.segments:
            if (
                abs(existing.start_s - seg.start_s) <= MATCH_TOLERANCE_S
                and abs(existing.end_s - seg.end_s) <= MATCH_TOLERANCE_S
            ):
                new_segments.append(seg)
                replaced = True
            else:
                new_segments.append(existing)
        if not replaced:
            new_segments.append(seg)
        # Keep storage stable & diff-friendly.
        new_segments.sort(key=lambda s: (s.start_s, s.end_s))
        page.segments = new_segments
        self._persist(page)

    def clear_bvid(self, uid: int, bvid: str) -> None:
        """Remove the entire cache directory for a bvid (call on success)."""
        d = self._bvid_dir(uid, bvid)
        if not d.exists():
            return
        with self._lock:
            for child in d.glob("*.json"):
                try:
                    child.unlink()
                except OSError as exc:  # pragma: no cover — best-effort
                    logger.warning(
                        "asr_cache_clear_failed_file",
                        extra={"path": str(child), "error": str(exc)},
                    )
            # Non-empty (race with another writer) or already gone — fine.
            with contextlib.suppress(OSError):
                d.rmdir()

    # -- internal ---------------------------------------------------------

    def _persist(self, page: _PageCache) -> None:
        path = self._page_path(page.uid, page.bvid, page.page_index)
        payload = {
            "version": CACHE_VERSION,
            "uid": page.uid,
            "bvid": page.bvid,
            "page_index": page.page_index,
            "segments": [s.to_dict() for s in page.segments],
            "updated_at": int(time.time() * 1000),
        }
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
