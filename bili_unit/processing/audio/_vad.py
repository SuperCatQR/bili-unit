# audio/_vad — VAD-aware audio segmentation.
#
# Why this exists:
#   Hard-cutting at a fixed segment length (e.g. 830 s for the 8192-token
#   MiMo budget) chops sentences mid-word, which degrades ASR quality.
#   This module finds silence gaps via Silero VAD (ONNX runtime, no torch
#   dependency via ``pysilero-vad``) and chooses cut points at the middle of
#   those gaps so neither side of a cut starts/ends in the middle of speech.
#
# Two layers:
#   1. ``detect_speech_segments(path, …)`` — decode the audio file to 16 kHz
#      mono PCM via ffmpeg, run pysilero-vad over fixed-size chunks, and
#      collapse adjacent speech chunks into ``(start_s, end_s)`` regions.
#   2. ``pick_split_points(duration, speech, *, max_seg, min_seg, overlap)``
#      — pure function over the detection result.  Greedy: from t0=0,
#      look for a silence gap (between consecutive speech regions) inside
#      ``[t0+min_seg, t0+max_seg]``; cut at the gap midpoint.  When no gap
#      exists in the window (continuous speech) we hard-cut at t0+max_seg
#      and the next segment starts ``overlap_sec`` earlier — text-domain
#      stitching reconciles the overlap downstream.
#
# Both layers are independently tested: pick_split_points has no ONNX/ffmpeg
# dependency at all; detect_speech_segments is mocked at the ffmpeg + VAD
# boundary in unit tests.

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import ConvertError
from ._ffmpeg import resolve_ffmpeg

logger = logging.getLogger("bili.processing.audio.vad")

# Silero VAD operates on 16 kHz mono 16-bit PCM with a fixed chunk size
# (512 samples = 32 ms at 16 kHz, per pysilero-vad).  These constants are
# only used by ``detect_speech_segments`` — pick_split_points is independent.
_SAMPLE_RATE = 16_000
_BYTES_PER_SAMPLE = 2  # 16-bit
_FFMPEG_TIMEOUT = 300


def pick_split_points(
    duration_seconds: float,
    speech_segments: list[tuple[float, float]],
    *,
    max_seg: float,
    min_seg: float = 60.0,
    overlap_sec: float = 2.5,
) -> list[tuple[float, float]]:
    """Choose ``(start_s, end_s)`` segment plan respecting silence gaps.

    Args:
        duration_seconds: total audio length.
        speech_segments: VAD-detected speech regions as ``(start_s, end_s)``
            in playback order, non-overlapping.  Gaps between consecutive
            regions are silence; the implicit gap before the first region
            and after the last region count too.
        max_seg: hard upper bound on a single segment's length (e.g. derived
            from MiMo's 8192-token budget — ~830 s at 6.5 tok/s).
        min_seg: lower bound when looking for a silence gap.  We won't cut
            earlier than t0+min_seg even if a gap exists there — too-short
            segments waste API overhead.
        overlap_sec: when no silence gap is available in the search window
            and we have to hard-cut, the next segment starts this many
            seconds earlier than the cut point.  Stitch_transcripts is
            responsible for deduplicating the overlap in the text domain.

    Returns:
        Ordered list of ``(start_s, end_s)`` cut plan that fully covers
        ``[0, duration_seconds]``.  Returns a single span when
        ``duration_seconds <= max_seg``.
    """
    if duration_seconds <= 0:
        return []
    if duration_seconds <= max_seg:
        return [(0.0, duration_seconds)]
    if min_seg <= 0:
        min_seg = 60.0
    if min_seg >= max_seg:
        # Pathological config — fall back to even hard-cut (no preference).
        min_seg = max_seg / 2

    # Sort + sanitize speech segments. Out-of-range or zero-length entries
    # are dropped; we don't try to merge near-adjacent regions because
    # callers already do that (detect_speech_segments collapses chunks).
    speech = sorted(
        (max(0.0, s), min(duration_seconds, e))
        for s, e in speech_segments
        if e > s and e > 0.0 and s < duration_seconds
    )

    # Build silence gaps as (gap_start, gap_end) — including head + tail.
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in speech:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration_seconds:
        gaps.append((cursor, duration_seconds))

    plan: list[tuple[float, float]] = []
    t0 = 0.0
    while duration_seconds - t0 > max_seg:
        window_lo = t0 + min_seg
        window_hi = t0 + max_seg

        # Find the gap whose midpoint lies in [window_lo, window_hi]; prefer
        # the latest qualifying gap so each segment is as long as possible
        # (within budget) — fewer ASR calls, less stitching.
        chosen_cut: float | None = None
        for gs, ge in gaps:
            mid = 0.5 * (gs + ge)
            if mid < window_lo:
                continue
            if mid > window_hi:
                break  # gaps are sorted; no later gap qualifies either
            chosen_cut = mid

        if chosen_cut is not None:
            plan.append((t0, chosen_cut))
            t0 = chosen_cut
        else:
            # No silence in the window — hard-cut at window_hi and start the
            # next segment ``overlap_sec`` earlier so stitch can reconcile.
            cut = window_hi
            plan.append((t0, cut))
            t0 = max(t0 + 1.0, cut - overlap_sec)  # never go backwards
            logger.info(
                "vad_hard_cut_overlap",
                extra={
                    "cut_s": cut,
                    "next_start_s": t0,
                    "overlap_s": cut - t0,
                },
            )

    plan.append((t0, duration_seconds))
    return plan


# --- detection layer -------------------------------------------------------


async def _decode_to_pcm(input_path: Path, ffmpeg_setting: str) -> bytes:
    """Decode arbitrary audio to raw 16 kHz mono 16-bit PCM bytes via ffmpeg.

    Returns the entire PCM stream in memory.  Sized cost: ~2 bytes/sample *
    16000 samples/s * duration_s.  A 60-min clip ≈ 115 MB, acceptable for
    typical bilibili content (most pages are <30 min).
    """
    ffmpeg = resolve_ffmpeg(ffmpeg_setting)
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(input_path),
        "-vn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(_SAMPLE_RATE),
        "-ac",
        "1",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_FFMPEG_TIMEOUT,
        )
    except TimeoutError:
        raise ConvertError(f"ffmpeg PCM decode timed out after {_FFMPEG_TIMEOUT}s: {input_path}") from None
    if proc.returncode != 0:
        raise ConvertError(
            f"ffmpeg PCM decode failed (rc={proc.returncode}) for {input_path}: "
            f"{stderr.decode(errors='replace')[-500:]}"
        )
    return stdout


def _pcm_to_speech_segments(
    pcm: bytes,
    *,
    threshold: float,
    min_silence_sec: float,
    min_speech_sec: float,
) -> list[tuple[float, float]]:
    """Run pysilero-vad over PCM bytes; return collapsed speech regions.

    Per-chunk VAD returns a probability; chunks with prob >= threshold are
    speech.  Adjacent speech chunks are merged.  Speech regions shorter than
    ``min_speech_sec`` and silence runs shorter than ``min_silence_sec`` are
    smoothed out (a too-short silence inside speech is bridged; a too-short
    speech region between long silences is dropped).
    """
    # Lazy import — keeps test collection fast and lets `pip install` users
    # opt out of audio extras if they only need the fetching half.
    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()
    chunk_bytes = vad.chunk_bytes()
    chunk_samples = vad.chunk_samples()
    chunk_seconds = chunk_samples / _SAMPLE_RATE

    # Walk the PCM in chunk_bytes-sized windows; partial trailing window is
    # zero-padded and treated as silence (negligible at 32 ms grain).
    flags: list[bool] = []
    pos = 0
    while pos + chunk_bytes <= len(pcm):
        prob = vad(pcm[pos : pos + chunk_bytes])
        flags.append(prob >= threshold)
        pos += chunk_bytes

    if not flags:
        return []

    # Collapse runs of consecutive True flags into (start_chunk, end_chunk).
    raw_runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for idx, is_speech in enumerate(flags):
        if is_speech and run_start is None:
            run_start = idx
        elif not is_speech and run_start is not None:
            raw_runs.append((run_start, idx))
            run_start = None
    if run_start is not None:
        raw_runs.append((run_start, len(flags)))

    # Bridge silences shorter than min_silence_sec (often breath / brief pause
    # mid-sentence); we never want to cut there.
    min_silence_chunks = max(1, int(round(min_silence_sec / chunk_seconds)))
    bridged: list[list[int]] = []
    for s, e in raw_runs:
        if bridged and s - bridged[-1][1] < min_silence_chunks:
            bridged[-1][1] = e
        else:
            bridged.append([s, e])

    # Drop speech runs shorter than min_speech_sec (likely noise spikes).
    min_speech_chunks = max(1, int(round(min_speech_sec / chunk_seconds)))
    kept = [(s, e) for s, e in bridged if e - s >= min_speech_chunks]

    return [(s * chunk_seconds, e * chunk_seconds) for s, e in kept]


async def detect_speech_segments(
    input_path: str | Path,
    *,
    ffmpeg_setting: str = "auto",
    threshold: float = 0.3,
    min_silence_sec: float = 0.4,
    min_speech_sec: float = 0.2,
) -> list[tuple[float, float]]:
    """Detect speech regions in *input_path* via Silero VAD (ONNX).

    Args:
        input_path: any audio file ffmpeg can decode (mp3, m4s, wav, …).
        ffmpeg_setting: passed through to :func:`resolve_ffmpeg`.
        threshold: VAD probability above which a chunk counts as speech.
            Default 0.3 (per project convention — slightly more sensitive
            than the upstream 0.5 default to avoid missing softer speech in
            mixed-music videos).
        min_silence_sec: silence runs shorter than this are bridged inside
            adjacent speech regions (i.e. not exposed as a candidate gap).
        min_speech_sec: speech runs shorter than this are discarded as
            spurious.

    Returns:
        Sorted, non-overlapping ``(start_s, end_s)`` speech regions.
    """
    pcm = await _decode_to_pcm(Path(input_path), ffmpeg_setting)
    # Run the (CPU-bound, sync) VAD in a thread so we don't block the loop.
    return await asyncio.to_thread(
        _pcm_to_speech_segments,
        pcm,
        threshold=threshold,
        min_silence_sec=min_silence_sec,
        min_speech_sec=min_speech_sec,
    )
