# transform/_base — TransformHandler protocol + WorkItem dataclass.
#
# Per docs/design/processing.md §6.5: a transform handler defines
# how to extract work items from a fetching endpoint's raw_payload and
# how to convert a single item into a structured result dict.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WorkItem:
    """A single transform unit, addressable by (item_type, item_id).

    item_data carries whatever sub-slice of raw_payload the handler needs
    in order to compute the result. Keeping this self-contained makes the
    transform call a pure function.
    """

    item_type: str
    item_id: str
    item_data: dict[str, Any]


class TransformHandler(Protocol):
    """Protocol for a single transform pipeline handler.

    Implementations live next to this file (one per item_type) and register
    themselves in :mod:`._registry`.
    """

    item_type: str
    """Unique identifier for this handler, e.g. ``"video_metadata"``."""

    source_endpoints: tuple[str, ...]
    """fetching endpoint names whose raw_payload feeds this handler.

    Phase-0 scanning only emits work items when *all* listed endpoints have
    a SUCCESS row available via ``fetching.query``.
    """

    def extract_items(self, raw_payloads: dict[str, dict]) -> list[WorkItem]:
        """Walk raw_payloads (endpoint_name → raw_payload) and emit work items."""
        ...

    def transform(self, item: WorkItem) -> dict[str, Any]:
        """Compute the structured result for one work item. No I/O."""
        ...
