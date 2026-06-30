# Worker page-envelope fallback tests.
#
# CHO-46 QA found that the currently pinned real bili-worker build returns
# ``{"raw_payload": ...}`` for fetch_page without ``is_last_page`` /
# ``next_request``. The main side must infer page-strategy pagination until the
# worker dependency advances to a build that emits the full envelope itself.

from __future__ import annotations

from dataclasses import dataclass

from bili_unit.fetching import _infer_page_pagination


@dataclass
class _Spec:
    pagination_strategy: str = "page"
    items_path: str | None = "list.vlist"


def test_infer_page_pagination_returns_next_request_for_non_last_videos_page() -> None:
    raw_payload = {
        "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
        "page": {"pn": 1, "ps": 30, "count": 42},
    }

    is_last_page, next_request = _infer_page_pagination(
        _Spec(), raw_payload, {"pn": 1, "ps": 30},
    )

    assert is_last_page is False
    assert next_request == {"pn": 2, "ps": 30}


def test_infer_page_pagination_marks_last_page_when_count_reached() -> None:
    raw_payload = {
        "list": {"vlist": [{"bvid": "BV1"}, {"bvid": "BV2"}]},
        "page": {"pn": 2, "ps": 30, "count": 42},
    }

    is_last_page, next_request = _infer_page_pagination(
        _Spec(), raw_payload, {"pn": 2, "ps": 30},
    )

    assert is_last_page is True
    assert next_request is None


def test_infer_page_pagination_keeps_extra_request_params() -> None:
    raw_payload = {
        "data": [{"id": 1}],
        "curPage": 1,
        "pageCount": 2,
        "totalSize": 2,
    }

    is_last_page, next_request = _infer_page_pagination(
        _Spec(items_path="data"), raw_payload, {"pn": 1, "ps": 1, "order": "pubdate"},
    )

    assert is_last_page is False
    assert next_request == {"pn": 2, "ps": 1, "order": "pubdate"}


async def test_assemble_worker_fallback_fetches_second_page(monkeypatch, tmp_path) -> None:
    """Runner should keep paginating when a real worker omits envelope fields."""
    import bili_unit.fetching._bilibili_adapter as adapter
    import bili_unit.fetching.worker_client as worker_client_module
    from bili_unit import fetching as fetching_module
    from bili_unit._db import UidContext
    from bili_unit._env import BiliSettings
    from bili_unit.fetching import TaskStatus
    from bili_unit.fetching._store import FetchingStore
    from bili_unit.tests.fake_worker import FakeWorker

    monkeypatch.setattr(adapter, "init_http_backend", lambda *_args, **_kwargs: None)

    fake = FakeWorker()
    fake.responses["credential_open"] = {"credential_ref": "cred-1"}
    calls: list[dict] = []

    async def _fetch_page(uid, endpoint, cred_ref, request_params, timeout=None):
        calls.append(dict(request_params))
        pn = request_params.get("pn", 1)
        if pn == 1:
            return {
                "raw_payload": {
                    "list": {"vlist": [{"bvid": "BV_PAGE_1"}]},
                    "page": {"pn": 1, "ps": 30, "count": 42},
                },
            }
        return {
            "raw_payload": {
                "list": {"vlist": [{"bvid": "BV_PAGE_2"}]},
                "page": {"pn": 2, "ps": 30, "count": 42},
            },
        }

    fake.fetch_page = _fetch_page  # type: ignore[method-assign]
    monkeypatch.setattr(worker_client_module, "WorkerClient", lambda: fake)

    settings = BiliSettings(
        bili_db_dir=str(tmp_path),
        bili_fetching_max_retries=0,
        bili_fetching_global_qps=1000.0,
        bili_fetching_endpoint_qps=1000.0,
    )
    cmd = await fetching_module.assemble(settings, use_worker=True)
    try:
        result = await cmd.fetch_uid(400, endpoints=["videos"], mode="full")
    finally:
        await cmd.close()

    assert result.status == TaskStatus.SUCCESS
    assert calls == [{"pn": 1, "ps": 30}, {"pn": 2, "ps": 30}]

    ctx = UidContext(uid=400, root=tmp_path)
    await ctx.open()
    try:
        payload = await FetchingStore(ctx).get_raw_payload("videos")
    finally:
        await ctx.close()
    assert payload == {
        "pages": [
            {"list": {"vlist": [{"bvid": "BV_PAGE_1"}]}, "page": {"pn": 1, "ps": 30, "count": 42}},
            {"list": {"vlist": [{"bvid": "BV_PAGE_2"}]}, "page": {"pn": 2, "ps": 30, "count": 42}},
        ],
    }
