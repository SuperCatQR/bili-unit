from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
import pytest_asyncio

from bili_unit.fetching import EndpointDTO, EndpointStatus
from bili_unit.parsing.data import ParsingDataStore
from bili_unit.parsing.keys import _item_key
from bili_unit.parsing.materializer import ParsingMaterializer
from bili_unit.parsing.query import ParsingQuery
from bili_unit.parsing.specs import MODEL_ORDER, get_spec, iter_specs

# Most tests in this file target the legacy ParsingDataStore + ParsingQuery
# pair plus the old ParsingMaterializer(data, fetch_qry) signature. Phase
# 3.2 swapped the materializer to (ctx, parse_store, fetch_store) and
# replaced the file-KV store; only the spec-registry assertion below is
# infrastructure-free and survives unchanged. Per-function skips keep that
# survivor running while we wait for the Phase 6 rewrite.
_LEGACY_INFRA = pytest.mark.skip(
    reason="moved to Phase 6 rewrite — parsing internals reshaped in Phase 3.2",
)


@pytest_asyncio.fixture
async def parsing_store(tmp_path):
    store = ParsingDataStore(tmp_path / "parsing")
    await store.open()
    yield store
    await store.close()


def test_parsing_specs_register_existing_models():
    assert MODEL_ORDER == (
        "user_profile",
        "video_work",
        "video_subtitle",
        "article_post",
        "opus_post",
        "dynamic_event",
    )

    handlers = {spec.name: spec.materializer_handler for spec in iter_specs()}
    assert set(handlers) == set(MODEL_ORDER)
    assert handlers["user_profile"] == "_parse_user_profile"
    assert handlers["dynamic_event"] == "_parse_dynamic_events"
    assert handlers["video_subtitle"] == "_parse_video_subtitle"
    assert all(handler.startswith("_parse_") for handler in handlers.values())
    assert get_spec("user_profile").singleton is True
    assert isinstance(get_spec("video_work").parser_cls().__name__, str)
    assert get_spec("dynamic_event").parser_cls().__name__ == "DynamicPost"


@pytest.mark.asyncio
@_LEGACY_INFRA
async def test_query_generic_get_item_and_list_items(parsing_store):
    uid = 4242
    await parsing_store.put(_item_key(uid, "article_post", "cv1"), {"id": "cv1", "title": "one"})
    await parsing_store.put(_item_key(uid, "article_post", "cv2"), {"id": "cv2", "title": "two"})

    query = ParsingQuery(parsing_store)

    item = await query.get_item(uid, "article_post", "cv1")
    assert item is not None
    assert item["id"] == "cv1"
    assert item["title"] == "one"

    items = await query.list_items(uid, "article_post")
    assert {item["id"] for item in items} == {"cv1", "cv2"}

    legacy_items = await query.list_articles(uid)
    assert {item["id"] for item in legacy_items} == {"cv1", "cv2"}


@pytest.mark.asyncio
@_LEGACY_INFRA
async def test_query_legacy_list_methods_read_canonical_model_dirs(parsing_store):
    uid = 4343
    await parsing_store.put(
        _item_key(uid, "video_work", "BV1xx"),
        {"_model_name": "video_work", "bvid": "BV1xx"},
    )
    await parsing_store.put(
        _item_key(uid, "article_post", "100"),
        {"_model_name": "article_post", "cvid": "100"},
    )
    await parsing_store.put(
        _item_key(uid, "opus_post", "200"),
        {"_model_name": "opus_post", "opus_id": "200"},
    )
    await parsing_store.put(
        _item_key(uid, "dynamic_event", "dyn300"),
        {"_model_name": "dynamic_event", "dynamic_id": "dyn300"},
    )

    query = ParsingQuery(parsing_store)

    assert [item["bvid"] for item in await query.list_video_details(uid)] == ["BV1xx"]
    assert [item["cvid"] for item in await query.list_articles(uid)] == ["100"]
    assert [item["opus_id"] for item in await query.list_opus(uid)] == ["200"]
    assert [item["dynamic_id"] for item in await query.list_dynamics(uid)] == ["dyn300"]


@pytest.mark.asyncio
@_LEGACY_INFRA
async def test_query_generic_rejects_unknown_model(parsing_store):
    query = ParsingQuery(parsing_store)

    with pytest.raises(KeyError):
        await query.get_item(1, "missing_model", "x")

    with pytest.raises(KeyError):
        await query.list_items(1, "missing_model")


@pytest.mark.asyncio
@_LEGACY_INFRA
async def test_incremental_user_profile_skips_existing_key(parsing_store):
    uid = 5151
    existing = {"_model_name": "user_profile", "mid": uid, "name": "already parsed"}
    await parsing_store.put(_item_key(uid, "user_profile", str(uid)), existing)

    fetch_query = MagicMock()
    fetch_query.get_endpoint = AsyncMock()
    materializer = ParsingMaterializer(parsing_store, fetch_query)

    count = await materializer.parse_model(uid, "user_profile", "incremental")

    assert count == 0
    fetch_query.get_endpoint.assert_not_awaited()
    stored = await parsing_store.get(_item_key(uid, "user_profile", str(uid)))
    assert stored is not None
    assert stored["name"] == existing["name"]


@pytest.mark.asyncio
@_LEGACY_INFRA
async def test_incremental_video_work_skips_existing_items(parsing_store):
    uid = 6262
    await parsing_store.put(
        _item_key(uid, "video_work", "BVold"),
        {"_model_name": "video_work", "bvid": "BVold", "title": "old title"},
    )

    async def get_video_detail(_uid: int, bvid: str) -> EndpointDTO:
        return EndpointDTO(
            uid=_uid,
            endpoint="video_detail",
            status=EndpointStatus.SUCCESS,
            available=True,
            raw_payload={
                "info": {
                    "bvid": bvid,
                    "title": f"{bvid} fresh",
                    "pages": [],
                    "stat": {},
                    "owner": {},
                },
                "tags": [],
            },
        )

    fetch_query = MagicMock()
    fetch_query.list_video_details = AsyncMock(
        return_value=[
            ("BVold", EndpointStatus.SUCCESS),
            ("BVnew", EndpointStatus.SUCCESS),
        ],
    )
    fetch_query.get_video_detail = AsyncMock(side_effect=get_video_detail)
    materializer = ParsingMaterializer(parsing_store, fetch_query)

    count = await materializer.parse_model(uid, "video_work", "incremental")

    assert count == 1
    assert fetch_query.get_video_detail.await_args_list == [call(uid, "BVnew")]
    old_item = await parsing_store.get(_item_key(uid, "video_work", "BVold"))
    new_item = await parsing_store.get(_item_key(uid, "video_work", "BVnew"))
    assert old_item is not None
    assert old_item["title"] == "old title"
    assert new_item is not None
    assert new_item["title"] == "BVnew fresh"
