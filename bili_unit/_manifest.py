# bili_unit/_manifest — per-uid cross-stage summary blob.
#
# Aggregates the three stage tasks (fetching / parsing / processing) into one
# dict so downstream consumers can answer "what does this uid look like?"
# without scanning three separate stores. The manifest itself is a side-effect
# of the write path: ``BiliCommand`` invokes :func:`compute_manifest` after
# each stage run and persists the result via :func:`write_manifest`.
#
# This module is internal — see ``docs/api.md`` (Internal section). The CLI
# ``manifest <uid>`` is the public surface; consumers should not depend on the
# Python helpers directly.

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .query import BiliQuery


_MANIFEST_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

async def compute_manifest(uid: int, qry: BiliQuery) -> dict[str, Any]:
    """Aggregate the three stage tasks + counts + cost + completeness into one dict.

    Read-only: never writes anything; the caller is responsible for persisting
    the returned dict via :func:`write_manifest`. Stages that have not been
    assembled or have not run for the uid render as ``None`` in their slot.
    """
    fetching_task = await qry.fetching.get_task(uid)
    parsing_task = (
        await qry.parsing.get_task(uid) if qry._parsing is not None else None
    )
    processing_task = (
        await qry.processing.get_task(uid) if qry._processing is not None else None
    )

    fetching_summary = _summarise_fetching(fetching_task)
    parsing_summary = await _summarise_parsing(uid, qry, parsing_task)
    processing_summary = await _summarise_processing(uid, qry, processing_task)
    cost = await _summarise_cost(uid, qry)
    completeness = await _summarise_completeness(uid, qry)

    return {
        "uid": uid,
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "computed_at": int(time.time() * 1000),
        "fetching": fetching_summary,
        "parsing": parsing_summary,
        "processing": processing_summary,
        "cost": cost,
        "completeness": completeness,
    }


def write_manifest(uid: int, manifest_dir: str | Path, manifest: dict) -> None:
    """Persist a manifest dict to disk; creates the parent directory as needed."""
    p = _manifest_path(uid, manifest_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_manifest(uid: int, manifest_dir: str | Path) -> dict | None:
    """Read a manifest from disk; ``None`` if the file does not exist."""
    p = _manifest_path(uid, manifest_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def delete_manifest(uid: int, manifest_dir: str | Path) -> bool:
    """Remove a uid's manifest. Returns True iff a file was actually deleted."""
    p = _manifest_path(uid, manifest_dir)
    if p.exists():
        p.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _manifest_path(uid: int, manifest_dir: str | Path) -> Path:
    return Path(manifest_dir) / f"{uid}.json"


def _summarise_fetching(task: Any | None) -> dict[str, Any] | None:
    """Reduce a fetching :class:`TaskDTO` to a manifest-friendly dict."""
    if task is None:
        return None

    success_count = 0
    failed_count = 0
    for ep_dto in task.endpoints.values():
        status_value = ep_dto.status.value
        if status_value == "SUCCESS":
            success_count += 1
        elif status_value.startswith("FAILED") or status_value == "PARTIAL_ITEM":
            failed_count += 1

    return {
        "status": task.status.value,
        "endpoint_count": len(task.endpoints),
        "success_count": success_count,
        "failed_count": failed_count,
        "failed_item_ids": list(task.failed_item_ids),
        "updated_at": task.updated_at,
    }


async def _summarise_parsing(
    uid: int,
    qry: BiliQuery,
    task: Any | None,
) -> dict[str, Any] | None:
    """Reduce a parsing :class:`ParsingTaskDTO` to a manifest-friendly dict."""
    if task is None:
        return None

    models_summary: dict[str, dict[str, Any]] = {}
    for name, model_dto in task.models.items():
        complete_count = await _count_complete_items(qry, uid, name)
        models_summary[name] = {
            "count": model_dto.count,
            "complete_count": complete_count,
            "status": model_dto.status.value,
        }

    images_summary = None
    if task.images is not None:
        img = task.images
        images_summary = {
            "total": img.total,
            "ok": img.ok,
            "skipped": img.skipped,
            "failed": img.failed,
        }

    return {
        "status": task.status.value,
        "models": models_summary,
        "images": images_summary,
        "failed_item_ids": list(task.failed_item_ids),
        "updated_at": task.updated_at,
    }


async def _count_complete_items(qry: BiliQuery, uid: int, model: str) -> int:
    """Count parsed items for ``model`` whose ``is_complete`` is True.

    Falls back to 0 on any read failure so the manifest writer never crashes
    a stage finalisation.
    """
    try:
        items = await qry.parsing.list_items(uid, model)
    except Exception:  # noqa: BLE001 — manifest must not break stage runs
        return 0
    n = 0
    for it in items:
        if isinstance(it, dict) and it.get("is_complete") is True:
            n += 1
    return n


async def _summarise_processing(
    uid: int,
    qry: BiliQuery,
    task: Any | None,
) -> dict[str, Any] | None:
    """Reduce a processing :class:`ProcessingTaskDTO` to a manifest-friendly dict."""
    if task is None:
        return None

    pipelines_summary: dict[str, dict[str, Any]] = {}
    for pname, pdto in task.pipelines.items():
        per_item: dict[str, dict[str, int]] = {}
        for it_type, counts in pdto.items.items():
            per_item[it_type] = dict(counts)
        # For the audio pipeline, attribute completed work to subtitle vs ASR
        # by inspecting the per-item ``transcription_source``.
        if pname == "audio":
            sub_count, asr_count = await _count_audio_sources(qry, uid)
            transcription = per_item.get("transcription")
            if transcription is not None:
                transcription["subtitle_source"] = sub_count
                transcription["asr_source"] = asr_count
        pipelines_summary[pname] = {
            "status": pdto.status.value,
            **per_item,
        }

    return {
        "status": task.status.value,
        "pipelines": pipelines_summary,
        "failed_item_ids": list(task.failed_item_ids),
        "updated_at": task.updated_at,
    }


async def _count_audio_sources(qry: BiliQuery, uid: int) -> tuple[int, int]:
    """Return ``(subtitle_count, asr_count)`` over completed audio items."""
    try:
        items = await qry.processing.list_items(uid, "audio")
    except Exception:  # noqa: BLE001
        return 0, 0
    sub = 0
    asr = 0
    for it in items:
        result = it.result or {}
        source = result.get("transcription_source")
        if source == "subtitle":
            sub += 1
        elif source == "asr":
            asr += 1
    return sub, asr


async def _summarise_cost(uid: int, qry: BiliQuery) -> dict[str, Any] | None:
    """Aggregate per-item ASR cost into a single block for the uid."""
    if qry._processing is None:
        return None
    try:
        items = await qry.processing.list_items(uid, "audio")
    except Exception:  # noqa: BLE001
        return None
    if not items:
        return None

    total_audio_tokens = 0
    total_seconds = 0
    asr_calls = 0
    cache_hits = 0
    subtitle_count = 0

    for it in items:
        result = it.result or {}
        cost = result.get("cost") or {}
        with contextlib.suppress(TypeError, ValueError):
            total_audio_tokens += int(cost.get("audio_tokens", 0) or 0)
        with contextlib.suppress(TypeError, ValueError):
            total_seconds += int(cost.get("seconds", 0) or 0)
        with contextlib.suppress(TypeError, ValueError):
            cache_hits += int(cost.get("cache_hits", 0) or 0)
        if result.get("transcription_source") == "subtitle":
            subtitle_count += 1
        elif result.get("transcription_source") == "asr":
            asr_calls += 1

    return {
        "total_audio_tokens": total_audio_tokens,
        "total_seconds": total_seconds,
        "asr_calls": asr_calls,
        "cache_hits": cache_hits,
        "subtitle_count": subtitle_count,
    }


async def _summarise_completeness(
    uid: int,
    qry: BiliQuery,
) -> dict[str, float] | None:
    """Per-model completeness ratios + video subtitle coverage.

    Returns ``None`` when parsing has never run for the uid. Individual
    models with zero items are omitted from the dict to avoid 0/0 entries.
    """
    if qry._parsing is None:
        return None

    out: dict[str, float] = {}

    for model in (
        "user_profile",
        "video_work",
        "article_post",
        "opus_post",
        "dynamic_event",
    ):
        try:
            items = await qry.parsing.list_items(uid, model)
        except Exception:  # noqa: BLE001
            continue
        total = len(items)
        if total == 0:
            continue
        complete = sum(
            1 for it in items if isinstance(it, dict) and it.get("is_complete") is True
        )
        out[model] = complete / total

    # video_subtitle coverage: fraction of video_work bvids that have a
    # video_subtitle entry. Only meaningful when at least one video exists.
    try:
        videos = await qry.parsing.list_items(uid, "video_work")
        subtitles = await qry.parsing.list_items(uid, "video_subtitle")
    except Exception:  # noqa: BLE001
        videos, subtitles = [], []
    total_videos = len(videos)
    if total_videos > 0:
        out["video_subtitle"] = len(subtitles) / total_videos

    return out if out else None


__all__ = [
    "compute_manifest",
    "delete_manifest",
    "read_manifest",
    "write_manifest",
]
