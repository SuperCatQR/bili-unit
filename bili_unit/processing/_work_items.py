# bili_unit.processing._work_items — derive ASR work items from raw_payload.
#
# The ASR stage used to read pre-parsed ``video`` / ``video_page`` rows from
# a separate parsing store. Now that there is no parsing layer, the runner
# pulls bvid + pages metadata directly from the raw ``video_detail``
# responses.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._db import Connection


_VIDEO_DETAIL_ENDPOINT = "video_detail"


async def list_audio_work_items(conn: Connection) -> dict[str, list[dict]]:
    """Return ``{bvid: [page_dict, ...]}`` from ``raw_payload(video_detail)``.

    Each page dict has the same shape the audio runner has historically used::

        {"page_index": int, "cid": int, "duration": int, "part": str}

    ``page_index`` is zero-based; videos with no usable ``info.pages`` array
    fall back to a single synthesised page so the runner still has somewhere
    to dispatch its worker.
    """
    rows = await conn.fetch_all(
        "SELECT item_id, payload FROM raw_payload WHERE endpoint = ? AND item_id <> '' ORDER BY item_id ASC",
        (_VIDEO_DETAIL_ENDPOINT,),
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        bvid = str(row["item_id"])
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            info = {}
        raw_pages = info.get("pages")
        pages: list[dict] = []
        if isinstance(raw_pages, list) and raw_pages:
            for page_no, p in enumerate(raw_pages, start=1):
                if not isinstance(p, dict):
                    continue
                pages.append(
                    {
                        "page_index": page_no - 1,
                        "cid": int(p.get("cid") or 0),
                        "duration": int(p.get("duration") or 0),
                        "part": str(p.get("part") or ""),
                    }
                )
        if not pages:
            # Single-page fallback so single-part videos and videos missing
            # the ``pages`` array still get one work item.
            pages = [
                {
                    "page_index": 0,
                    "cid": int(info.get("cid") or 0),
                    "duration": int(info.get("duration") or 0),
                    "part": "",
                }
            ]
        out[bvid] = pages
    return out


__all__ = ["list_audio_work_items"]
