# transform/_base — TransformHandler protocol + WorkItem dataclass.
#
# After the parsing-layer migration, processing transform handlers consume
# typed dataclass objects from parsing (stored as dicts in the parsing store),
# NOT raw fetching payloads.  The runner reads from ParsingQuery and creates
# WorkItems; each handler's transform() converts one typed-object dict into
# a structured result dict.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WorkItem:
    """A single transform unit, addressable by (item_type, item_id).

    item_data carries the typed-object dict from the parsing store
    (e.g. a VideoDetail or Article serialised via to_dict()).
    Keeping this self-contained makes the transform call a pure function.
    """

    item_type: str
    item_id: str
    item_data: dict[str, Any]


class TransformHandler(Protocol):
    """Protocol for a single transform pipeline handler.

    Implementations live next to this file (one per item_type) and register
    themselves in :mod:`._registry`.

    The runner handles item discovery from the parsing store; each handler
    only needs to implement :meth:`transform`.
    """

    item_type: str
    """Unique identifier for this handler, e.g. ``"video_metadata"``."""

    source_endpoints: tuple[str, ...]
    """fetching endpoint names that conceptually feed this handler.

    Retained for metadata / progress tracking.  The runner reads from the
    parsing store rather than directly from fetching.
    """

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Compute the structured result for one work item. No I/O."""
        ...
