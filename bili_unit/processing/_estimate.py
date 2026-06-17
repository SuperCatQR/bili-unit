"""Processing budget estimation helpers.

The estimate is intentionally conservative and cheap: it only needs the
candidate work items that the audio pipeline already discovered.  It does not
open audio files or call the ASR backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any


@dataclass(frozen=True)
class ProcessingEstimate:
    """Estimated ASR work for one processing run."""

    item_count: int
    page_count: int
    audio_seconds: float
    audio_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_count": self.item_count,
            "page_count": self.page_count,
            "audio_seconds": self.audio_seconds,
            "audio_tokens": self.audio_tokens,
        }


def estimate_audio_work(items: list[Any], *, tokens_per_second: float) -> ProcessingEstimate:
    """Estimate audio seconds/tokens from discovered work items.

    ``items`` are ``WorkItem``-shaped objects carrying
    ``item_data["pages"][].duration``. Missing or malformed durations count as
    zero rather than blocking the run.
    """
    page_count = 0
    seconds = 0.0
    for item in items:
        pages = item.item_data.get("pages", []) if hasattr(item, "item_data") else []
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_count += 1
            try:
                seconds += float(page.get("duration") or 0)
            except (TypeError, ValueError):
                continue
    tokens = ceil(seconds * max(tokens_per_second, 0.0))
    return ProcessingEstimate(
        item_count=len(items),
        page_count=page_count,
        audio_seconds=seconds,
        audio_tokens=tokens,
    )


def estimate_exceeds_budget(
    estimate: ProcessingEstimate,
    *,
    max_audio_seconds: float | None,
    max_audio_tokens: int | None,
) -> list[str]:
    """Return budget labels exceeded by ``estimate``."""
    exceeded: list[str] = []
    if max_audio_seconds is not None and estimate.audio_seconds > max_audio_seconds:
        exceeded.append("audio_seconds")
    if max_audio_tokens is not None and estimate.audio_tokens > max_audio_tokens:
        exceeded.append("audio_tokens")
    return exceeded


__all__ = [
    "ProcessingEstimate",
    "estimate_audio_work",
    "estimate_exceeds_budget",
]
