# runner._audio_work — per-page audio pipeline stages.
#
# Pure helpers split out of the original ``_do_audio_work``:
#   * audio_download_page  — CDN m4s download (returns audio_info dict)
#   * audio_convert_page   — ffmpeg → mp3 (+ token-budget / VAD / size split)
#   * audio_transcribe_page — ASR per segment with resume cache
#
# These functions are deliberately stateless — they take the dependencies
# they need explicitly so the orchestrator (``_AudioMixin._do_audio_work``)
# stays in control of temp cleanup and per-bvid cache invalidation.

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import ASRAPIError
from ..audio._asr_cache import ASRCacheStore, CachedSegment
from ..audio._converter import Mp3Segment, convert_at_points
from ..audio._stitch import stitch_transcripts

if TYPE_CHECKING:
    from ..._env import BiliSettings
    from ...observability import RunReporter
    from ..audio._asr_backend import ASRBackend
    from ..audio._downloader import AudioDownloader

ConvertFn = Callable[..., Awaitable[list[Mp3Segment]]]

logger = logging.getLogger("bili.processing.runner")


async def audio_download_page(
    downloader: AudioDownloader,
    bvid: str,
    page_index: int,
    quality: str,
    m4s_path: Path,
) -> dict[str, Any]:
    """Download CDN audio for one page.

    Returns the ``audio_info`` dict from the CDN resolver (carries ``url``,
    ``duration`` and any backend-specific metadata).  The downloaded m4s is
    written to ``m4s_path``.
    """
    audio_info = await downloader.get_audio_url(
        bvid, page_index=page_index, quality=quality,
    )
    await downloader.download_to_file(audio_info["url"], str(m4s_path), bvid)
    return audio_info


async def audio_convert_page(
    m4s_path: Path,
    out_dir: Path,
    page_duration_for_split: float | None,
    settings: BiliSettings,
    *,
    convert_fn: ConvertFn,
) -> list[Mp3Segment]:
    """Convert m4s → mp3 segments according to ``settings``.

    The split decision (token budget / VAD / size fallback) lives in
    ``convert_fn`` (defaults to :func:`bili_unit.processing.audio.convert_single`
    in production; injected by the runner so tests can substitute without
    module-level patching).
    """
    return await convert_fn(
        m4s_path,
        out_dir,
        max_file_size_mb=settings.bili_processing_asr_max_file_size_mb,
        segment_minutes=settings.bili_processing_audio_max_segment_minutes,
        ffmpeg_setting=settings.bili_processing_ffmpeg_path,
        duration_seconds=page_duration_for_split,
        max_input_tokens=settings.bili_processing_asr_max_input_tokens,
        tokens_per_second=settings.bili_processing_asr_tokens_per_second,
        max_segment_seconds=settings.bili_processing_asr_max_segment_seconds,
        use_vad=settings.bili_processing_asr_use_vad,
        vad_threshold=settings.bili_processing_asr_vad_threshold,
        vad_min_silence_sec=settings.bili_processing_asr_vad_min_silence_sec,
        vad_min_speech_sec=settings.bili_processing_asr_vad_min_speech_sec,
        vad_min_seg_sec=settings.bili_processing_asr_vad_min_seg_sec,
        vad_overlap_sec=settings.bili_processing_asr_vad_overlap_sec,
    )


async def audio_transcribe_page(
    asr_backend: ASRBackend,
    asr_cache: ASRCacheStore | None,
    uid: int,
    bvid: str,
    page_index: int,
    segments: list[Mp3Segment],
    asr_language: str,
    *,
    high_risk_split_enabled: bool = False,
    high_risk_split_seconds: float = 30.0,
    high_risk_min_segment_seconds: float = 10.0,
    empty_segment_skip_seconds: float = 5.0,
    rate_limit_max_attempts: int = 3,
    rate_limit_retry_delays: tuple[float, ...] = (10.0, 30.0, 60.0),
    ffmpeg_setting: str = "auto",
    reporter: RunReporter | None = None,
) -> dict[str, Any]:
    """Run ASR on each segment, threading results through the resume cache.

    On retry, segments whose ``(start_s, end_s)`` match a previously cached
    entry skip the API call and reuse the stored text.  Each fresh ASR
    result is persisted before moving on — a crash mid-page never re-bills
    already-paid segments.

    Returns a dict with ``text`` (stitched), ``segment_duration_sum``,
    ``got_any_segment_duration``, ``cache_hits``, ``segments``,
    ``audio_tokens_total``, ``asr_seconds_total``, and
    ``fresh_segment_count`` — one ``segments`` entry per input mp3 segment
    (in order) carrying its source-timeline range, transcribed text,
    ASR-reported duration, and model id.  Cache hits and fresh ASR calls
    both contribute to ``segments`` (and to ``audio_tokens_total`` /
    ``asr_seconds_total``); ``fresh_segment_count`` counts only the
    segments that actually hit the ASR backend on this run.
    """
    page_cache = (
        asr_cache.load_page(uid, bvid, page_index)
        if asr_cache is not None else None
    )

    segment_duration_sum: float = 0.0
    got_any_segment_duration = False
    cache_hits = 0
    segment_texts: list[str] = []
    segments_out: list[dict] = []
    backend_model = getattr(asr_backend, "model", "")
    audio_tokens_total: int = 0
    asr_seconds_total: float = 0.0
    fresh_segment_count: int = 0
    empty_segment_skips: int = 0
    high_risk_segment_skips: int = 0

    for seg in segments:
        cached: CachedSegment | None = (
            asr_cache.find(page_cache, seg.start_s, seg.end_s)
            if asr_cache is not None and page_cache is not None
            else None
        )
        if cached is not None:
            cache_hits += 1
            segment_texts.append(cached.text)
            if cached.duration is not None:
                segment_duration_sum += cached.duration
                got_any_segment_duration = True
                asr_seconds_total += float(cached.duration)
            if cached.audio_tokens is not None:
                audio_tokens_total += int(cached.audio_tokens)
            segments_out.append({
                "start_s": cached.start_s,
                "end_s": cached.end_s,
                "text": cached.text,
                "duration": cached.duration,
                "model": cached.model,
            })
            continue

        seg_results = await _transcribe_segment_with_high_risk_fallback(
            asr_backend,
            asr_cache,
            page_cache,
            seg,
            asr_language,
            high_risk_split_enabled=high_risk_split_enabled,
            split_seconds=high_risk_split_seconds,
            min_segment_seconds=high_risk_min_segment_seconds,
            empty_segment_skip_seconds=empty_segment_skip_seconds,
            rate_limit_max_attempts=rate_limit_max_attempts,
            rate_limit_retry_delays=rate_limit_retry_delays,
            ffmpeg_setting=ffmpeg_setting,
            reporter=reporter,
            uid=uid,
            bvid=bvid,
            page_index=page_index,
        )
        for item in seg_results:
            segment_texts.append(item["text"])
            if item.get("empty_skip"):
                empty_segment_skips += 1
            elif item.get("high_risk_skip"):
                high_risk_segment_skips += 1
            elif item.get("cache_hit"):
                cache_hits += 1
            else:
                fresh_segment_count += 1
            duration = item.get("duration")
            if duration is not None:
                segment_duration_sum += float(duration)
                got_any_segment_duration = True
                asr_seconds_total += float(duration)
            audio_tokens = item.get("audio_tokens")
            if audio_tokens is not None:
                audio_tokens_total += int(audio_tokens)
            segments_out.append({
                "start_s": item["start_s"],
                "end_s": item["end_s"],
                "text": item["text"],
                "duration": (
                    float(duration) if duration is not None else None
                ),
                "model": item.get("model") or backend_model,
                **({
                    "empty_skip": True,
                } if item.get("empty_skip") else {}),
                **({
                    "high_risk_skip": True,
                    "error": item.get("error"),
                } if item.get("high_risk_skip") else {}),
            })

    if cache_hits > 0:
        logger.info(
            "asr_cache_hits",
            extra={
                "uid": uid, "bvid": bvid,
                "page_index": page_index,
                "hits": cache_hits,
                "total_segments": len(segments),
            },
        )

    # When a clip is split into multiple segments we may have a small
    # overlap between adjacent pieces (forced hard-cut on continuous-speech
    # windows where VAD found no silence gap).  ``stitch_transcripts``
    # deduplicates that overlap; for VAD cuts (zero overlap) it degenerates
    # to a plain space join.
    text = stitch_transcripts(segment_texts)
    return {
        "text": text,
        "segment_duration_sum": segment_duration_sum,
        "got_any_segment_duration": got_any_segment_duration,
        "cache_hits": cache_hits,
        "segments": segments_out,
        "audio_tokens_total": audio_tokens_total,
        "asr_seconds_total": asr_seconds_total,
        "fresh_segment_count": fresh_segment_count,
        "empty_segment_skips": empty_segment_skips,
        "high_risk_segment_skips": high_risk_segment_skips,
    }


def _is_high_risk_error(exc: Exception) -> bool:
    return (
        isinstance(exc, ASRAPIError)
        and "high risk" in str(exc).lower()
    )


def _is_empty_transcript_error(exc: Exception) -> bool:
    return (
        isinstance(exc, ASRAPIError)
        and "empty transcription text" in str(exc).lower()
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    if not isinstance(exc, ASRAPIError):
        return False
    text = str(exc).lower()
    return (
        "429" in text
        or "too many requests" in text
        or "rate limit" in text
        or "limitation" in text
    )


def _skipped_segment(
    seg: Mp3Segment,
    *,
    reason: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    item = {
        "start_s": seg.start_s,
        "end_s": seg.end_s,
        "text": "",
        "duration": seg.end_s - seg.start_s,
        "model": "",
        "audio_tokens": 0,
        "cache_hit": False,
    }
    if reason == "empty":
        item["empty_skip"] = True
    elif reason == "high_risk":
        item["high_risk_skip"] = True
        item["error"] = str(error) if error is not None else None
    return item


def _split_segment_ranges(
    start_s: float,
    end_s: float,
    split_seconds: float,
) -> list[tuple[float, float]]:
    duration = max(0.0, end_s - start_s)
    if duration <= 0 or split_seconds <= 0:
        return []
    count = max(1, math.ceil(duration / split_seconds))
    return [
        (
            start_s + idx * split_seconds,
            min(end_s, start_s + (idx + 1) * split_seconds),
        )
        for idx in range(count)
    ]


async def _transcribe_segment_once(
    asr_backend: ASRBackend,
    asr_cache: ASRCacheStore | None,
    page_cache: Any,
    seg: Mp3Segment,
    asr_language: str,
) -> dict[str, Any]:
    audio_bytes = seg.path.read_bytes()
    asr_result = await asr_backend.transcribe(
        audio_bytes, mime_type="audio/mp3", language=asr_language,
    )
    audio_tokens = getattr(asr_result, "audio_tokens", None)
    model = getattr(asr_backend, "model", "") or asr_result.model
    item = {
        "start_s": seg.start_s,
        "end_s": seg.end_s,
        "text": asr_result.text,
        "duration": (
            float(asr_result.duration)
            if asr_result.duration is not None else None
        ),
        "model": model,
        "audio_tokens": audio_tokens,
        "cache_hit": False,
    }
    if asr_cache is not None and page_cache is not None:
        asr_cache.upsert(page_cache, CachedSegment(
            start_s=seg.start_s,
            end_s=seg.end_s,
            text=asr_result.text,
            language=asr_language,
            duration=asr_result.duration,
            model=model,
            audio_tokens=audio_tokens,
        ))
    return item


async def _transcribe_segment_once_with_rate_limit_retry(
    asr_backend: ASRBackend,
    asr_cache: ASRCacheStore | None,
    page_cache: Any,
    seg: Mp3Segment,
    asr_language: str,
    *,
    max_attempts: int,
    retry_delays: tuple[float, ...],
    reporter: RunReporter | None = None,
    uid: int | None = None,
    bvid: str | None = None,
    page_index: int | None = None,
) -> dict[str, Any]:
    attempts = max(1, int(max_attempts))
    delays = tuple(max(float(delay), 0.0) for delay in retry_delays) or (0.0,)
    for attempt in range(1, attempts + 1):
        try:
            return await _transcribe_segment_once(
                asr_backend, asr_cache, page_cache, seg, asr_language,
            )
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt >= attempts:
                raise
            delay = delays[min(attempt - 1, len(delays) - 1)]
            logger.info(
                "asr_segment_rate_limit_retry",
                extra={
                    "segment": str(seg.path),
                    "start_s": seg.start_s,
                    "end_s": seg.end_s,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "delay_s": delay,
                    "error": str(exc),
                },
            )
            if reporter is not None:
                await reporter.emit(
                    "asr.segment.rate_limited",
                    stage="processing",
                    pipeline="audio",
                    item_type="transcription",
                    item_id=bvid,
                    data={
                        "uid": uid,
                        "bvid": bvid,
                        "page_index": page_index,
                        "segment": str(seg.path),
                        "start_s": seg.start_s,
                        "end_s": seg.end_s,
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "delay_s": delay,
                        "error": str(exc),
                    },
                )
            if delay > 0:
                await asyncio.sleep(delay)
    raise ASRAPIError("ASR segment retry exhausted unexpectedly")


async def _transcribe_segment_with_high_risk_fallback(
    asr_backend: ASRBackend,
    asr_cache: ASRCacheStore | None,
    page_cache: Any,
    seg: Mp3Segment,
    asr_language: str,
    *,
    high_risk_split_enabled: bool,
    split_seconds: float,
    min_segment_seconds: float,
    empty_segment_skip_seconds: float,
    rate_limit_max_attempts: int,
    rate_limit_retry_delays: tuple[float, ...],
    ffmpeg_setting: str,
    reporter: RunReporter | None = None,
    uid: int | None = None,
    bvid: str | None = None,
    page_index: int | None = None,
) -> list[dict[str, Any]]:
    high_risk_exc: Exception | None = None
    try:
        return [
            await _transcribe_segment_once_with_rate_limit_retry(
                asr_backend, asr_cache, page_cache, seg, asr_language,
                max_attempts=rate_limit_max_attempts,
                retry_delays=rate_limit_retry_delays,
                reporter=reporter,
                uid=uid,
                bvid=bvid,
                page_index=page_index,
            )
        ]
    except Exception as exc:
        if (
            _is_empty_transcript_error(exc)
            and (seg.end_s - seg.start_s) <= max(float(empty_segment_skip_seconds), 0.0)
        ):
            logger.info(
                "asr_empty_segment_skipped",
                extra={
                    "segment": str(seg.path),
                    "start_s": seg.start_s,
                    "end_s": seg.end_s,
                    "duration_s": seg.end_s - seg.start_s,
                },
            )
            if reporter is not None:
                await reporter.emit(
                    "asr.segment.empty_skipped",
                    stage="processing",
                    pipeline="audio",
                    item_type="transcription",
                    item_id=bvid,
                    data={
                        "uid": uid,
                        "bvid": bvid,
                        "page_index": page_index,
                        "segment": str(seg.path),
                        "start_s": seg.start_s,
                        "end_s": seg.end_s,
                        "duration_s": seg.end_s - seg.start_s,
                    },
                )
            return [{
                **_skipped_segment(seg, reason="empty"),
                "model": getattr(asr_backend, "model", ""),
            }]
        high_risk_exc = exc
        should_split = (
            high_risk_split_enabled
            and _is_high_risk_error(exc)
            and seg.end_s > seg.start_s
            and (seg.end_s - seg.start_s) > max(float(min_segment_seconds), 0.0)
        )
        if not should_split:
            if high_risk_split_enabled and _is_high_risk_error(exc):
                logger.warning(
                    "asr_high_risk_segment_skipped",
                    extra={
                        "segment": str(seg.path),
                        "start_s": seg.start_s,
                        "end_s": seg.end_s,
                        "duration_s": seg.end_s - seg.start_s,
                        "min_segment_seconds": float(min_segment_seconds),
                    },
                )
                if reporter is not None:
                    await reporter.emit(
                        "asr.segment.high_risk_skipped",
                        stage="processing",
                        level="WARNING",
                        pipeline="audio",
                        item_type="transcription",
                        item_id=bvid,
                        data={
                            "uid": uid,
                            "bvid": bvid,
                            "page_index": page_index,
                            "segment": str(seg.path),
                            "start_s": seg.start_s,
                            "end_s": seg.end_s,
                            "duration_s": seg.end_s - seg.start_s,
                            "min_segment_seconds": float(min_segment_seconds),
                            "error": str(exc),
                        },
                    )
                return [{
                    **_skipped_segment(seg, reason="high_risk", error=exc),
                    "model": getattr(asr_backend, "model", ""),
                }]
            raise

    duration = seg.end_s - seg.start_s
    next_split_seconds = min(
        max(float(split_seconds), float(min_segment_seconds)),
        max(float(min_segment_seconds), duration / 2.0),
    )
    points = _split_segment_ranges(seg.start_s, seg.end_s, next_split_seconds)
    if len(points) <= 1:
        raise high_risk_exc or ASRAPIError("high-risk split produced no segments")

    logger.info(
        "asr_high_risk_split_retry",
        extra={
            "segment": str(seg.path),
            "start_s": seg.start_s,
            "end_s": seg.end_s,
            "pieces": len(points),
            "split_seconds": next_split_seconds,
        },
    )
    if reporter is not None:
        await reporter.emit(
            "asr.segment.high_risk_split",
            stage="processing",
            pipeline="audio",
            item_type="transcription",
            item_id=bvid,
            data={
                "uid": uid,
                "bvid": bvid,
                "page_index": page_index,
                "segment": str(seg.path),
                "start_s": seg.start_s,
                "end_s": seg.end_s,
                "pieces": len(points),
                "split_seconds": next_split_seconds,
            },
        )
    out_dir = seg.path.parent / f"{seg.path.stem}_high_risk_split"
    local_points = [
        (start_s - seg.start_s, end_s - seg.start_s)
        for start_s, end_s in points
    ]
    paths = await convert_at_points(seg.path, out_dir, local_points, ffmpeg_setting)

    out: list[dict[str, Any]] = []
    for path, (start_s, end_s) in zip(paths, points, strict=True):
        sub_seg = Mp3Segment(path, start_s, end_s)
        cached: CachedSegment | None = (
            asr_cache.find(page_cache, sub_seg.start_s, sub_seg.end_s)
            if asr_cache is not None and page_cache is not None
            else None
        )
        if cached is not None:
            out.append({
                "start_s": cached.start_s,
                "end_s": cached.end_s,
                "text": cached.text,
                "duration": cached.duration,
                "model": cached.model,
                "audio_tokens": cached.audio_tokens,
                "cache_hit": True,
            })
            continue
        out.extend(await _transcribe_segment_with_high_risk_fallback(
            asr_backend,
            asr_cache,
            page_cache,
            sub_seg,
            asr_language,
            high_risk_split_enabled=high_risk_split_enabled,
            split_seconds=max(float(min_segment_seconds), next_split_seconds / 2.0),
            min_segment_seconds=float(min_segment_seconds),
            empty_segment_skip_seconds=float(empty_segment_skip_seconds),
            rate_limit_max_attempts=int(rate_limit_max_attempts),
            rate_limit_retry_delays=tuple(rate_limit_retry_delays),
            ffmpeg_setting=ffmpeg_setting,
            reporter=reporter,
            uid=uid,
            bvid=bvid,
            page_index=page_index,
        ))
    return out
