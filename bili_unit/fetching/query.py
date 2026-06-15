# query — fetching read-only view; returns DTOs, never exposes store internals.

import logging

from . import (
    EndpointDTO,
    EndpointStatus,
    FetchingErrorDTO,
    TaskDTO,
    TaskStatus,
)
from .data import DataStore
from .error import ErrorStore
from .keys import _fetch_key, _item_fetch_key, _progress_key, _task_key
from .task import TaskValue

logger = logging.getLogger("bili.fetching.query")


class Query:
    """Read-only interface to fetching results and errors."""

    def __init__(self, data: DataStore, error: ErrorStore) -> None:
        self._data = data
        self._error = error

    # -- task ----------------------------------------------------------------

    async def get_task(self, uid: int) -> TaskDTO | None:
        """Return the overall task DTO for a uid, or None."""
        d = await self._data.get(_task_key(uid))
        if d is None:
            return None
        tv = TaskValue.from_dict(d)

        endpoint_dtos: dict[str, EndpointDTO] = {}
        for ep_name, _entry in tv.endpoints.items():
            ep_dto = await self.get_endpoint(uid, ep_name)
            if ep_dto is not None:
                endpoint_dtos[ep_name] = ep_dto

        # Prefer the persisted ``failed_item_ids`` (runner writes it on
        # finalisation). Fall back to a live recomputation when the field is
        # missing — keeps query consistent with stale-on-disk task values that
        # predate the field.
        failed_item_ids = list(tv.failed_item_ids)
        if not failed_item_ids:
            failed_item_ids = await self._derive_failed_item_ids(uid, tv)

        return TaskDTO(
            uid=tv.uid,
            status=tv.status,
            endpoints=endpoint_dtos,
            created_at=tv.created_at,
            updated_at=tv.updated_at,
            failed_item_ids=failed_item_ids,
        )

    async def list_tasks(self) -> list[dict]:
        """Return a lightweight summary of all tasks in the store.

        Each entry contains: uid, status, updated_at, endpoint_count,
        and video_detail item counts (if applicable).
        """
        rows = await self._data.list_task_rows()
        results: list[dict] = []
        for uid, value in rows:
            try:
                status = TaskStatus(value.get("status", "PENDING"))
            except ValueError:
                status = TaskStatus.PENDING

            endpoints = value.get("endpoints", {})

            # Extract video_detail item progress if present
            vd_items = None
            vd_entry = endpoints.get("video_detail")
            if vd_entry and "item_progress" in vd_entry:
                ip = vd_entry["item_progress"]
                vd_items = f"{ip.get('completed', 0)}/{ip.get('total', 0)}"

            results.append({
                "uid": uid,
                "status": status,
                "updated_at": value.get("updated_at"),
                "created_at": value.get("created_at"),
                "endpoint_count": len(endpoints),
                "video_detail_items": vd_items,
            })

        return results

    async def get_endpoint(self, uid: int, endpoint: str) -> EndpointDTO | None:
        """Return a single endpoint's DTO."""
        fetch_d = await self._data.get(_fetch_key(uid, endpoint))
        progress_d = await self._data.get(_progress_key(uid, endpoint))
        errors = await self._error.list_by_uid(uid)
        ep_errors = [e for e in errors if e.endpoint == endpoint]

        status = EndpointStatus.PENDING
        raw_payload = None
        fetched_at = None

        if fetch_d is not None:
            status_str = fetch_d.get("status", "PENDING")
            try:
                status = EndpointStatus(status_str)
            except ValueError:
                status = EndpointStatus.PENDING
            raw_payload = fetch_d.get("raw_payload")
            fetched_at = fetch_d.get("fetched_at")
        else:
            # Runner writes endpoint status to task entry (not fetch_key)
            # for failures, RUNNING, etc.  Fall back to task entry so that
            # FAILED_EXHAUSTED / FAILED_PERMANENT / RUNNING are visible.
            task_d = await self._data.get(_task_key(uid))
            if task_d is not None:
                entry = task_d.get("endpoints", {}).get(endpoint)
                if entry is not None:
                    try:
                        status = EndpointStatus(entry.get("status", "PENDING"))
                    except ValueError:
                        status = EndpointStatus.PENDING

        available = (
            status == EndpointStatus.SUCCESS
            and raw_payload is not None
        )

        return EndpointDTO(
            uid=uid,
            endpoint=endpoint,
            status=status,
            available=available,
            raw_payload=raw_payload,
            fetched_at=fetched_at,
            progress=progress_d,
            errors=ep_errors,
        )

    async def list_errors(self, uid: int | None = None) -> list[FetchingErrorDTO]:
        """List errors, optionally filtered by uid."""
        return await self._error.list_errors(uid=uid)

    async def list_fanout_payloads(
        self, uid: int, endpoint: str,
    ) -> dict[str, dict]:
        """Return successful item-level payloads for a fan-out endpoint.

        The mapping is ``{item_id: raw_payload}``, and only SUCCESS items with a
        non-empty raw payload are returned.
        """
        pairs = await self.list_items(uid, endpoint)
        payloads: dict[str, dict] = {}
        for item_id, status in pairs:
            if status != EndpointStatus.SUCCESS:
                continue
            item_dto = await self.get_item(uid, endpoint, item_id)
            if item_dto is None or item_dto.raw_payload is None:
                continue
            payloads[item_id] = item_dto.raw_payload
        return payloads

    # -- generic item-level fan-out -----------------------------------------

    async def get_item(
        self, uid: int, endpoint: str, item_id: str,
    ) -> EndpointDTO | None:
        """Return a single item-level fan-out payload as an EndpointDTO."""
        key = _item_fetch_key(uid, endpoint, item_id)
        d = await self._data.get(key)
        if d is None:
            return None

        status_str = d.get("status", "PENDING")
        try:
            status = EndpointStatus(status_str)
        except ValueError:
            status = EndpointStatus.PENDING
        raw_payload = d.get("raw_payload")
        fetched_at = d.get("fetched_at")

        available = status == EndpointStatus.SUCCESS and raw_payload is not None

        return EndpointDTO(
            uid=uid,
            endpoint=endpoint,
            status=status,
            available=available,
            raw_payload=raw_payload,
            fetched_at=fetched_at,
        )

    async def list_items(
        self, uid: int, endpoint: str,
    ) -> list[tuple[str, EndpointStatus]]:
        """Return all stored item ids for a fan-out endpoint with their status.

        Does NOT load raw_payload, keeping memory usage low.
        """
        prefix = f"uid:{uid}:fetch:{endpoint}:"
        rows = await self._data.list_prefix(prefix)
        results: list[tuple[str, EndpointStatus]] = []
        for key, value in rows:
            item_id = key.split(":", 4)[-1] if ":" in key else key
            status_str = value.get("status", "PENDING")
            try:
                status = EndpointStatus(status_str)
            except ValueError:
                status = EndpointStatus.PENDING
            results.append((item_id, status))
        return results

    # -- compatibility helpers ---------------------------------------------

    async def get_video_detail(self, uid: int, bvid: str) -> EndpointDTO | None:
        """Return a single bvid's detail as an EndpointDTO, or None."""
        return await self.get_item(uid, "video_detail", bvid)

    async def list_video_details(self, uid: int) -> list[tuple[str, EndpointStatus]]:
        """Return all stored video_detail bvids with their status."""
        return await self.list_items(uid, "video_detail")

    async def get_article_detail(self, uid: int, cvid: str) -> EndpointDTO | None:
        """Return a single article's detail as an EndpointDTO, or None.

        Mirrors :meth:`get_video_detail`; ``cvid`` is the cv号 stringified.
        """
        return await self.get_item(uid, "article_detail", cvid)

    async def list_article_details(self, uid: int) -> list[tuple[str, EndpointStatus]]:
        """Return all stored article_detail cvids with their status."""
        return await self.list_items(uid, "article_detail")

    async def get_opus_detail(self, uid: int, opus_id: str) -> EndpointDTO | None:
        """Return a single opus's detail as an EndpointDTO, or None.

        Mirrors :meth:`get_article_detail`; ``opus_id`` is the图文 ID stringified.
        """
        return await self.get_item(uid, "opus_detail", opus_id)

    async def list_opus_details(self, uid: int) -> list[tuple[str, EndpointStatus]]:
        """Return all stored opus_detail opus_ids with their status."""
        return await self.list_items(uid, "opus_detail")

    async def get_article_list_detail(
        self, uid: int, rlid: str,
    ) -> EndpointDTO | None:
        """Return a single readlist's roster as an EndpointDTO, or None.

        Mirrors :meth:`get_article_detail`; ``rlid`` is the 文集 id stringified.
        The raw_payload here is the cvid roster (``{list, articles, author}``),
        not an article body.
        """
        return await self.get_item(uid, "article_list_detail", rlid)

    async def list_article_list_details(
        self, uid: int,
    ) -> list[tuple[str, EndpointStatus]]:
        """Return all stored article_list_detail rlids with their status."""
        return await self.list_items(uid, "article_list_detail")

    # -- internal helpers ---------------------------------------------------

    async def _derive_failed_item_ids(
        self, uid: int, tv: TaskValue,
    ) -> list[str]:
        """Compute ``failed_item_ids`` from the error store + task entries.

        Mirrors :meth:`Runner._collect_failed_item_ids`; lives here as a fallback
        for tasks whose persisted form predates the field. See ``TaskDTO``
        docstring for the encoding.

        Item-level errors are reconciled against the data store: an
        item that has since written a SUCCESS record is dropped, so a
        retry-to-success doesn't leave a stale entry behind.
        """
        ids: set[str] = set()
        try:
            errors = await self._error.list_by_uid(uid)
        except Exception:  # noqa: BLE001
            errors = []

        item_level_eps = {
            name for name, entry in tv.endpoints.items()
            if entry.item_progress is not None
        }

        # Pre-load successful item keys per item-level endpoint.
        succeeded_items: set[tuple[str, str]] = set()
        for ep in item_level_eps:
            try:
                rows = await self._data.list_prefix(f"uid:{uid}:fetch:{ep}:")
            except Exception:  # noqa: BLE001
                continue
            for _, v in rows:
                if isinstance(v, dict) and v.get("status") == "SUCCESS":
                    iid = v.get("item_id")
                    if iid:
                        succeeded_items.add((ep, str(iid)))

        for err in errors:
            ep = err.endpoint
            if not ep:
                continue
            detail = err.detail or {}
            item_id = detail.get("item_id") if isinstance(detail, dict) else None
            if item_id:
                if (ep, str(item_id)) in succeeded_items:
                    continue
                ids.add(f"{ep}:{item_id}")
            else:
                ids.add(ep)

        for name, entry in tv.endpoints.items():
            if entry.last_error_id is None:
                continue
            if entry.status in (
                EndpointStatus.SUCCESS,
                EndpointStatus.PARTIAL_ITEM,
            ):
                continue
            if name not in item_level_eps:
                ids.add(name)

        return sorted(ids)
