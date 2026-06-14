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
        "content_post",
    )

    handlers = {spec.name: spec.materializer_handler for spec in iter_specs()}
    assert set(handlers) == set(MODEL_ORDER)
    assert handlers["user_profile"] == "_parse_user_profile"
    assert handlers["content_post"] == "_parse_content_posts"
    assert handlers["video_subtitle"] == "_parse_video_subtitle"
    assert all(handler.startswith("_parse_") for handler in handlers.values())
    assert get_spec("user_profile").singleton is True
    assert isinstance(get_spec("video_work").parser_cls().__name__, str)
    assert get_spec("content_post").parser_cls().__name__ == "ContentPost"


@pytest.mark.asyncio
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
async def test_query_generic_rejects_unknown_model(parsing_store):
    query = ParsingQuery(parsing_store)

    with pytest.raises(KeyError):
        await query.get_item(1, "missing_model", "x")

    with pytest.raises(KeyError):
        await query.list_items(1, "missing_model")


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_content_post_materializer_merges_article_opus_dynamic(parsing_store):
    uid = 7373

    async def get_endpoint(_uid: int, endpoint: str) -> EndpointDTO | None:
        payloads = {
            "articles": {
                "pages": [
                    {
                        "articles": [
                            {
                                "id": 100,
                                "title": "Article list title",
                                "summary": "Article list summary",
                                "ctime": 1700000000,
                                "image_urls": ["article-list.jpg"],
                            }
                        ]
                    }
                ]
            },
            "opus": {
                "pages": [
                    {
                        "items": [
                            {
                                "opus_id": "200",
                                "title": "Opus list title",
                                "summary": "Opus list summary",
                                "pub_time": 1700000100,
                                "modules": {},
                            }
                        ]
                    }
                ]
            },
            "dynamics": {
                "pages": [
                    {
                        "items": [
                            {
                                "id_str": "dyn_article",
                                "type": "DYNAMIC_TYPE_ARTICLE",
                                "modules": {
                                    "module_dynamic": {
                                        "desc": {"text": "Article dynamic text"},
                                        "major": {
                                            "type": "MAJOR_TYPE_ARTICLE",
                                            "article": {
                                                "id": 100,
                                                "title": "Dynamic article title",
                                            },
                                        },
                                    }
                                },
                            },
                            {
                                "id_str": "dyn_draw",
                                "type": "DYNAMIC_TYPE_DRAW",
                                "modules": {
                                    "module_dynamic": {
                                        "desc": {"text": "Draw dynamic text"},
                                        "major": {
                                            "type": "MAJOR_TYPE_DRAW",
                                            "draw": {"items": [{"src": "draw.jpg"}]},
                                        },
                                    }
                                },
                            },
                        ]
                    }
                ]
            },
        }
        payload = payloads.get(endpoint)
        if payload is None:
            return None
        return EndpointDTO(
            uid=_uid,
            endpoint=endpoint,
            status=EndpointStatus.SUCCESS,
            available=True,
            raw_payload=payload,
        )

    async def list_fanout_payloads(_uid: int, endpoint: str) -> dict[str, dict]:
        return {
            "article_detail": {
                "100": {
                    "info": {"id": 100, "title": "Article detail title"},
                    "markdown": "# Article",
                    "content_json": [{"text": "Article body"}],
                }
            },
            "article_list_detail": {},
            "opus_detail": {
                "200": {
                    "info": {"item": {"title": "Opus detail title"}},
                    "markdown": "# Opus",
                    "images": [{"url": "opus-detail.jpg"}],
                }
            },
        }[endpoint]

    fetch_query = MagicMock()
    fetch_query.get_endpoint = AsyncMock(side_effect=get_endpoint)
    fetch_query.list_fanout_payloads = AsyncMock(side_effect=list_fanout_payloads)
    materializer = ParsingMaterializer(parsing_store, fetch_query)

    count = await materializer.parse_model(uid, "content_post", "full")

    assert count == 3
    query = ParsingQuery(parsing_store)
    rows = await query.list_items(uid, "content_post")
    by_key = {row["content_key"]: row for row in rows}
    assert set(by_key) == {"article:100", "opus:200", "dynamic:dyn_draw"}
    assert by_key["article:100"]["title"] == "Article detail title"
    assert by_key["article:100"]["text"] == "Article body"
    assert by_key["article:100"]["_cross_refs"]["dynamic_id"] == "dyn_article"
    assert by_key["opus:200"]["title"] == "Opus detail title"
    assert by_key["dynamic:dyn_draw"]["images"] == ["draw.jpg"]


@pytest.mark.asyncio
async def test_incremental_content_post_skips_existing_item(parsing_store):
    uid = 8383
    await parsing_store.put(
        _item_key(uid, "content_post", "article~100"),
        {"content_key": "article:100", "title": "old"},
    )

    fetch_query = MagicMock()
    fetch_query.get_endpoint = AsyncMock(
        return_value=EndpointDTO(
            uid=uid,
            endpoint="articles",
            status=EndpointStatus.SUCCESS,
            available=True,
            raw_payload={
                "pages": [{"articles": [{"id": 100, "title": "new"}]}],
            },
        ),
    )
    fetch_query.list_fanout_payloads = AsyncMock(return_value={})
    materializer = ParsingMaterializer(parsing_store, fetch_query)

    count = await materializer.parse_model(uid, "content_post", "incremental")

    assert count == 0
    stored = await parsing_store.get(_item_key(uid, "content_post", "article~100"))
    assert stored is not None
    assert stored["title"] == "old"
