# _endpoint_spec -- endpoint metadata types for fetching.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

PaginationStrategy = str  # "none" | "page" | "cursor" | "anchor" | "legacy_offset" | "oid" | "custom"


@dataclass
class EndpointSpec:
    name: str
    callable: Callable[..., Awaitable[dict]]
    credential_required: bool = False
    params_strategy: dict[str, Any] = field(default_factory=dict)
    pagination_strategy: PaginationStrategy = "none"
    rate_limit_key: str = ""
    item_id_path: str | None = None
    item_id_paths: list[str] | None = None
    items_path: str | None = None
    kind: str = "uid"
    source_endpoint: str | None = None
    extract_items: Callable[[dict], list[str]] | None = None
    skip_item: Callable[[dict], str | None] | None = None
    needs_parent_uid: bool = False
