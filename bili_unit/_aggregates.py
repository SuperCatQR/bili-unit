# bili_unit/_aggregates — cross-stage aggregate DTOs.
#
# Some read-side views combine results from more than one stage
# (e.g. video metadata from parsing + transcription from processing).
# Their DTOs live here, away from any single stage's __init__, so the
# stage modules don't have to import each other to type their outputs.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class VideoFullDTO:
    """Full per-video record combining parsing metadata + audio transcription.

    metadata: the parsed VideoDetail dict from parsing.query.get_video_detail
        (None if parsing has no record for this bvid).
    transcription: the audio ProcessingItemDTO from processing.query.get_item
        (None if processing has not transcribed this bvid yet).
    """

    bvid: str
    metadata: dict[str, Any] | None = None
    transcription: Any = None  # ProcessingItemDTO; Any avoids cyclic import


@dataclass
class VideoSummaryDTO:
    """Lightweight per-video summary used by listing pages."""

    bvid: str
    title: str
    has_transcription: bool
    duration: int | None = None


__all__ = ["VideoFullDTO", "VideoSummaryDTO"]
