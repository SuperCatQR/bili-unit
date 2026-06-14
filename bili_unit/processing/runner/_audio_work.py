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

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..audio._asr_cache import ASRCacheStore, CachedSegment
from ..audio._converter import Mp3Segment
from ..audio._stitch import stitch_transcripts

if TYPE_CHECKING:
    from ..._env import BiliSettings
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

        audio_bytes = seg.path.read_bytes()
        asr_result = await asr_backend.transcribe(
            audio_bytes, mime_type="audio/mp3", language=asr_language,
        )
        segment_texts.append(asr_result.text)
        fresh_segment_count += 1
        if asr_result.duration is not None:
            segment_duration_sum += float(asr_result.duration)
            got_any_segment_duration = True
            asr_seconds_total += float(asr_result.duration)
        seg_audio_tokens = getattr(asr_result, "audio_tokens", None)
        if seg_audio_tokens is not None:
            audio_tokens_total += int(seg_audio_tokens)
        segments_out.append({
            "start_s": seg.start_s,
            "end_s": seg.end_s,
            "text": asr_result.text,
            "duration": (
                float(asr_result.duration)
                if asr_result.duration is not None else None
            ),
            "model": backend_model,
        })

        # Persist immediately — the whole point of the cache.
        if asr_cache is not None and page_cache is not None:
            asr_cache.upsert(page_cache, CachedSegment(
                start_s=seg.start_s,
                end_s=seg.end_s,
                text=asr_result.text,
                language=asr_language,
                duration=asr_result.duration,
                model=backend_model,
                audio_tokens=seg_audio_tokens,
            ))

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
    }
