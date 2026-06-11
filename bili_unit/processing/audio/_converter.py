# audio/_converter — m4s → mp3 conversion + long-audio segmentation via ffmpeg.
#
# Per docs/design/processing.md §7.3:
#   - B站 CDN audio is m4s (DASH segment); MiMo ASR accepts mp3.
#   - Conversion: -ar 16000 -ac 1 (16 kHz mono — sufficient for speech ASR).
#   - Long videos: -segment_time {seconds} to split.
#   - Timeout: 5 minutes per ffmpeg invocation.
#
# Segmentation strategy (token-budget aware):
#   MiMo mimo-v2.5-asr has 8192-token context, audio ~6.5 token/s.  A 17-min
#   clip is only ~3 MB at 16 kHz q:a 9 (well under any reasonable size limit)
#   yet costs ~6500 input tokens — so splitting *only* on file size never
#   triggers and the API rejects the request.
#
#   ``compute_segment_seconds(duration, max_input_tokens, tokens_per_second)``
#   returns the longest segment length that keeps each chunk under the token
#   budget.  ``convert_single`` uses it when *duration_seconds* is known,
#   falling back to size-based splitting otherwise.

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path

from .. import ConvertError
from ._ffmpeg import resolve_ffmpeg

logger = logging.getLogger("bili.processing.audio.converter")

_FFMPEG_TIMEOUT = 300  # 5 minutes per invocation


def compute_segment_seconds(
    duration_seconds: float,
    max_input_tokens: int,
    tokens_per_second: float,
) -> int | None:
    """Return per-segment length (seconds) that keeps each chunk under budget.

    Args:
        duration_seconds: total audio length.
        max_input_tokens: token budget per ASR call (e.g. 5400 for MiMo's 8192
            context minus completion + overhead).
        tokens_per_second: empirical audio token rate (≈ 6.5 for MiMo at
            16 kHz mono).

    Returns:
        ``None`` if the full clip already fits the budget — caller should
        skip segmentation.  Otherwise, the largest integer second count that
        keeps each segment ≤ ``max_input_tokens``, never returning <60 s
        (a sane lower bound that prevents pathological splits when inputs
        are misconfigured).
    """
    if duration_seconds <= 0 or tokens_per_second <= 0 or max_input_tokens <= 0:
        return None
    estimated = math.ceil(duration_seconds * tokens_per_second)
    if estimated <= max_input_tokens:
        return None
    seg = int(max_input_tokens // tokens_per_second)
    return max(seg, 60)


async def convert_m4s_to_mp3(
    input_path: str | Path,
    output_path: str | Path,
    ffmpeg_setting: str = "auto",
) -> Path:
    """Convert a single m4s audio file to mp3 (16 kHz mono).

    Args:
        input_path: source m4s file.
        output_path: destination mp3 file.
        ffmpeg_setting: passed to :func:`resolve_ffmpeg`.

    Returns:
        The resolved *output_path*.

    Raises:
        ConvertError: when ffmpeg fails or the output is not created.
    """
    ffmpeg = resolve_ffmpeg(ffmpeg_setting)
    inp = Path(input_path)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg,
        "-y",
        "-i", str(inp),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-q:a", "9",
        str(out),
    ]
    logger.debug("convert_m4s_to_mp3", extra={"cmd": " ".join(cmd)})

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_FFMPEG_TIMEOUT,
        )
    except TimeoutError:
        raise ConvertError(
            f"ffmpeg conversion timed out after {_FFMPEG_TIMEOUT}s: {inp}"
        ) from None

    if proc.returncode != 0:
        raise ConvertError(
            f"ffmpeg failed (rc={proc.returncode}) for {inp}: "
            f"{stderr.decode(errors='replace')[-500:]}"
        )

    if not out.exists():
        raise ConvertError(f"ffmpeg produced no output for {inp}")

    return out


async def convert_and_segment(
    input_path: str | Path,
    output_dir: str | Path,
    segment_seconds: int = 480,
    ffmpeg_setting: str = "auto",
) -> list[Path]:
    """Convert m4s → mp3 and split into segments of *segment_seconds*.

    Output files are named ``{output_dir}/seg_000.mp3``, ``seg_001.mp3``, etc.

    Returns:
        Sorted list of segment file paths.

    Raises:
        ConvertError: when ffmpeg fails or no segments are produced.
    """
    ffmpeg = resolve_ffmpeg(ffmpeg_setting)
    inp = Path(input_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pattern = str(out_dir / "seg_%03d.mp3")
    cmd = [
        ffmpeg,
        "-y",
        "-i", str(inp),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-q:a", "9",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        pattern,
    ]
    logger.debug(
        "convert_and_segment",
        extra={"cmd": " ".join(cmd), "segment_seconds": segment_seconds},
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_FFMPEG_TIMEOUT,
        )
    except TimeoutError:
        raise ConvertError(
            f"ffmpeg segmentation timed out after {_FFMPEG_TIMEOUT}s: {inp}"
        ) from None

    if proc.returncode != 0:
        raise ConvertError(
            f"ffmpeg segmentation failed (rc={proc.returncode}) for {inp}: "
            f"{stderr.decode(errors='replace')[-500:]}"
        )

    segments = sorted(out_dir.glob("seg_*.mp3"))
    if not segments:
        raise ConvertError(f"ffmpeg produced no segments for {inp}")

    return segments


async def convert_single(
    input_path: str | Path,
    output_dir: str | Path,
    max_file_size_mb: int = 10,
    segment_minutes: int = 8,
    ffmpeg_setting: str = "auto",
    *,
    duration_seconds: float | None = None,
    max_input_tokens: int | None = None,
    tokens_per_second: float | None = None,
) -> list[Path]:
    """High-level entry point: convert m4s → mp3, segment if needed.

    Decision tree:

    1. Always converts to a single mp3 first.
    2. **Token budget** (preferred when ``duration_seconds`` and
       ``max_input_tokens`` and ``tokens_per_second`` are all set):
       computes per-segment length via :func:`compute_segment_seconds`.
       If the clip already fits, returns the single full mp3.
       Otherwise re-converts with the computed ``-segment_time``.
    3. **Size fallback** (when token info missing): if mp3 exceeds
       ``max_file_size_mb``, re-converts with ``segment_minutes * 60``.

    The token-budget path is what protects MiMo's 8192-token context;
    size-based splitting was demonstrated to never trigger for typical
    16 kHz mono q:a 9 mp3 — a 17-minute clip is only ~3 MB.
    """
    inp = Path(input_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: full conversion (always).
    full_mp3 = out_dir / "full.mp3"
    await convert_m4s_to_mp3(inp, full_mp3, ffmpeg_setting)

    # Step 2: prefer token-budget segmentation when caller supplied audio
    # duration + budget.  This is the path that protects MiMo's 8192 cap.
    if (
        duration_seconds is not None
        and max_input_tokens is not None
        and tokens_per_second is not None
    ):
        seg_secs = compute_segment_seconds(
            duration_seconds, max_input_tokens, tokens_per_second,
        )
        if seg_secs is None:
            return [full_mp3]
        logger.info(
            "audio_token_budget_split",
            extra={
                "duration_s": duration_seconds,
                "segment_s": seg_secs,
                "max_input_tokens": max_input_tokens,
            },
        )
        full_mp3.unlink(missing_ok=True)
        seg_dir = out_dir / "segments"
        return await convert_and_segment(
            inp, seg_dir, seg_secs, ffmpeg_setting,
        )

    # Step 3: size fallback for callers that did not provide duration.
    size_mb = full_mp3.stat().st_size / (1024 * 1024)
    if size_mb <= max_file_size_mb:
        return [full_mp3]

    logger.info(
        "audio_exceeds_size_limit",
        extra={
            "file": str(full_mp3),
            "size_mb": round(size_mb, 2),
            "limit_mb": max_file_size_mb,
        },
    )
    full_mp3.unlink(missing_ok=True)
    seg_dir = out_dir / "segments"
    segment_seconds = segment_minutes * 60
    return await convert_and_segment(
        inp, seg_dir, segment_seconds, ffmpeg_setting,
    )
