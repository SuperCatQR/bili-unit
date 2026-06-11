# query — fetching read-only view; returns DTOs, never exposes store internals.

import logging

from . import (
    EndpointDTO,
    EndpointStatus,
    ErrorDTO,
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

        return TaskDTO(
            uid=tv.uid,
            status=tv.status,
            endpoints=endpoint_dtos,
            created_at=tv.created_at,
            updated_at=tv.updated_at,
        )

    async def list_tasks(self) -> list[dict]:
        """Return a lightweight summary of all tasks in the store.

        Each entry contains: uid, status, updated_at, endpoint_count,
        and video_detail item counts (if applicable).
        """
        all_rows = await self._data.list_prefix("uid:")
        results: list[dict] = []
        for key, value in all_rows:
            if not key.endswith(":task"):
                continue
            # key format: uid:{uid}:task
            parts = key.split(":")
            if len(parts) != 3:
                continue
            try:
                uid = int(parts[1])
            except ValueError:
                continue

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

        results.sort(key=lambda x: x["uid"])
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

    async def list_errors(self, uid: int | None = None) -> list[ErrorDTO]:
        """List errors, optionally filtered by uid."""
        return await self._error.list_errors(uid=uid)

    # -- video_detail --------------------------------------------------------

    async def get_video_detail(self, uid: int, bvid: str) -> EndpointDTO | None:
        """Return a single bvid's detail as an EndpointDTO, or None."""
        key = _item_fetch_key(uid, "video_detail", bvid)
        d = await self._data.get(key)
        if d is None:
            return None

        status = EndpointStatus.PENDING
        raw_payload = None
        fetched_at = None

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
            endpoint="video_detail",
            status=status,
            available=available,
            raw_payload=raw_payload,
            fetched_at=fetched_at,
        )

    async def list_video_details(self, uid: int) -> list[tuple[str, EndpointStatus]]:
        """Return all stored video_detail bvids with their status.

        Does NOT load raw_payload (to keep memory usage low).
        """
        prefix = f"uid:{uid}:fetch:video_detail:"
        rows = await self._data.list_prefix(prefix)
        results: list[tuple[str, EndpointStatus]] = []
        for key, value in rows:
            # extract bvid from key: uid:{uid}:fetch:video_detail:{bvid}
            bvid = key.split(":", 4)[-1] if ":" in key else key
            status_str = value.get("status", "PENDING")
            try:
                status = EndpointStatus(status_str)
            except ValueError:
                status = EndpointStatus.PENDING
            results.append((bvid, status))
        return results

    # -- article_detail ------------------------------------------------------

    async def get_article_detail(self, uid: int, cvid: str) -> EndpointDTO | None:
        """Return a single article's detail as an EndpointDTO, or None.

        Mirrors :meth:`get_video_detail`; ``cvid`` is the cv号 stringified.
        """
        key = _item_fetch_key(uid, "article_detail", cvid)
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
            endpoint="article_detail",
            status=status,
            available=available,
            raw_payload=raw_payload,
            fetched_at=fetched_at,
        )

    async def list_article_details(self, uid: int) -> list[tuple[str, EndpointStatus]]:
        """Return all stored article_detail cvids with their status.

        Does NOT load raw_payload — same memory discipline as
        :meth:`list_video_details`.
        """
        prefix = f"uid:{uid}:fetch:article_detail:"
        rows = await self._data.list_prefix(prefix)
        results: list[tuple[str, EndpointStatus]] = []
        for key, value in rows:
            cvid = key.split(":", 4)[-1] if ":" in key else key
            status_str = value.get("status", "PENDING")
            try:
                status = EndpointStatus(status_str)
            except ValueError:
                status = EndpointStatus.PENDING
            results.append((cvid, status))
        return results

    # -- opus_detail ---------------------------------------------------------

    async def get_opus_detail(self, uid: int, opus_id: str) -> EndpointDTO | None:
        """Return a single opus's detail as an EndpointDTO, or None.

        Mirrors :meth:`get_article_detail`; ``opus_id`` is the图文 ID stringified.
        """
        key = _item_fetch_key(uid, "opus_detail", opus_id)
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
            endpoint="opus_detail",
            status=status,
            available=available,
            raw_payload=raw_payload,
            fetched_at=fetched_at,
        )

    async def list_opus_details(self, uid: int) -> list[tuple[str, EndpointStatus]]:
        """Return all stored opus_detail opus_ids with their status.

        Does NOT load raw_payload — same memory discipline as
        :meth:`list_video_details` / :meth:`list_article_details`.
        """
        prefix = f"uid:{uid}:fetch:opus_detail:"
        rows = await self._data.list_prefix(prefix)
        results: list[tuple[str, EndpointStatus]] = []
        for key, value in rows:
            opus_id = key.split(":", 4)[-1] if ":" in key else key
            status_str = value.get("status", "PENDING")
            try:
                status = EndpointStatus(status_str)
            except ValueError:
                status = EndpointStatus.PENDING
            results.append((opus_id, status))
        return results
