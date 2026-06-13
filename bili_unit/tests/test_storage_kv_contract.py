# Contract tests for bili_unit._storage.JsonKVStore.
#
# Both fetching and processing use the same JsonKVStore core; each ships its
# own KeyMapper for its key schema.  These tests parametrise over both mappers
# and exercise the shared CRUD / atomic helpers so the two stages stay
# behaviour-equivalent.

from pathlib import Path

import pytest

from bili_unit._storage import JsonKVStore, StorageError
from bili_unit.fetching.data import FetchingKeyMapper
from bili_unit.processing.data import ProcessingKeyMapper

# -- per-mapper key fixtures ------------------------------------------------

# Each entry: (name, mapper_factory, sample_keys_for_basic_crud,
# prefix_keys, prefix_to_match, expected_match_count, pair_keys).
_PARAMS = [
    pytest.param(
        FetchingKeyMapper,
        # CRUD test keys
        ("uid:7:fetch:videos", "uid:7:fetch:videos:BV001"),
        # list_prefix fixtures
        [
            ("uid:1:fetch:videos", {"x": 1}),
            ("uid:1:fetch:videos:BV001", {"x": 2}),
            ("uid:1:progress:videos", {"x": 3}),
            ("uid:2:fetch:videos", {"x": 4}),
        ],
        "uid:1:",
        3,
        # write_pair_locked keys (fetch + progress, idempotent commit pattern)
        ("uid:9:fetch:videos", "uid:9:progress:videos"),
        id="fetching",
    ),
    pytest.param(
        ProcessingKeyMapper,
        ("uid:7:proc:video_metadata:BVa", "uid:7:progress:transform"),
        [
            ("uid:1:proc:video_metadata:BVa", {"x": 1}),
            ("uid:1:proc:video_metadata:BVb", {"x": 2}),
            ("uid:1:progress:transform", {"x": 3}),
            ("uid:2:proc:video_metadata:BVc", {"x": 4}),
        ],
        "uid:1:",
        3,
        ("uid:9:proc:video_metadata:BVa", "uid:9:progress:transform"),
        id="processing",
    ),
]


@pytest.mark.parametrize(
    "mapper_factory, crud_keys, prefix_data, prefix, expected_count, pair_keys",
    _PARAMS,
)
@pytest.mark.asyncio
async def test_kv_basic_crud(
    tmp_path: Path,
    mapper_factory,
    crud_keys,
    prefix_data,
    prefix,
    expected_count,
    pair_keys,
):
    store = JsonKVStore(tmp_path / "kv", mapper_factory(), decode_error_cls=StorageError)
    await store.open()
    try:
        k1, k2 = crud_keys
        # put + get + updated_at stamp
        await store.put(k1, {"a": 1})
        v = await store.get(k1)
        assert v is not None
        assert v["a"] == 1
        assert "updated_at" in v

        # overwrite
        await store.put(k1, {"a": 2})
        assert (await store.get(k1))["a"] == 2

        # missing
        assert await store.get(k2) is None

        # delete (silent on missing)
        await store.delete(k2)  # already missing — no error
        await store.delete(k1)
        assert await store.get(k1) is None
    finally:
        await store.close()


@pytest.mark.parametrize(
    "mapper_factory, crud_keys, prefix_data, prefix, expected_count, pair_keys",
    _PARAMS,
)
@pytest.mark.asyncio
async def test_kv_list_prefix(
    tmp_path: Path,
    mapper_factory,
    crud_keys,
    prefix_data,
    prefix,
    expected_count,
    pair_keys,
):
    store = JsonKVStore(tmp_path / "kv", mapper_factory(), decode_error_cls=StorageError)
    await store.open()
    try:
        for key, value in prefix_data:
            await store.put(key, value)
        rows = await store.list_prefix(prefix)
        assert len(rows) == expected_count
        # All returned keys actually start with the requested prefix.
        for key, _ in rows:
            assert key.startswith(prefix)
        # Round-trip: every persisted key under the prefix must reappear.
        expected_keys = {k for k, _ in prefix_data if k.startswith(prefix)}
        actual_keys = {k for k, _ in rows}
        assert actual_keys == expected_keys
    finally:
        await store.close()


@pytest.mark.parametrize(
    "mapper_factory, crud_keys, prefix_data, prefix, expected_count, pair_keys",
    _PARAMS,
)
@pytest.mark.asyncio
async def test_kv_update_in_place(
    tmp_path: Path,
    mapper_factory,
    crud_keys,
    prefix_data,
    prefix,
    expected_count,
    pair_keys,
):
    store = JsonKVStore(tmp_path / "kv", mapper_factory(), decode_error_cls=StorageError)
    await store.open()
    try:
        k1, _ = crud_keys
        # mutator on missing key returning None is a no-op.
        await store.update_in_place(k1, lambda cur: None)
        assert await store.get(k1) is None

        # seed and mutate.
        await store.put(k1, {"counter": 0, "tag": "x"})

        def _bump(cur):
            assert cur is not None
            cur["counter"] = cur["counter"] + 1
            return cur

        await store.update_in_place(k1, _bump)
        await store.update_in_place(k1, _bump)
        v = await store.get(k1)
        assert v["counter"] == 2
        assert v["tag"] == "x"
        assert "updated_at" in v

        # mutator returning None deletes the key.
        await store.update_in_place(k1, lambda cur: None)
        assert await store.get(k1) is None

        # stamp_updated_at=False preserves caller's updated_at.
        await store.put(k1, {"v": 1})
        await store.update_in_place(
            k1,
            lambda cur: {"v": (cur or {}).get("v", 0) + 1, "updated_at": 12345},
            stamp_updated_at=False,
        )
        assert (await store.get(k1))["updated_at"] == 12345
    finally:
        await store.close()


@pytest.mark.parametrize(
    "mapper_factory, crud_keys, prefix_data, prefix, expected_count, pair_keys",
    _PARAMS,
)
@pytest.mark.asyncio
async def test_kv_write_pair_locked(
    tmp_path: Path,
    mapper_factory,
    crud_keys,
    prefix_data,
    prefix,
    expected_count,
    pair_keys,
):
    store = JsonKVStore(tmp_path / "kv", mapper_factory(), decode_error_cls=StorageError)
    await store.open()
    try:
        ka, kb = pair_keys
        await store.write_pair_locked(ka, {"page": 1}, kb, {"done": False})
        va = await store.get(ka)
        vb = await store.get(kb)
        assert va is not None and vb is not None
        assert va["page"] == 1
        assert vb["done"] is False
        # both stamped with the same updated_at.
        assert va["updated_at"] == vb["updated_at"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_kv_decode_error_raises_injected_class(tmp_path: Path):
    """Corrupted JSON must raise the caller-supplied decode_error_cls."""

    class MyDataError(Exception):
        pass

    store = JsonKVStore(
        tmp_path / "kv",
        FetchingKeyMapper(),
        decode_error_cls=MyDataError,
    )
    await store.open()
    try:
        # Put a valid value so the path exists, then corrupt the file.
        await store.put("uid:1:fetch:videos", {"a": 1})
        # Locate the file via the mapper so we don't depend on internal layout.
        path = FetchingKeyMapper().to_path(store.base, "uid:1:fetch:videos")
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(MyDataError):
            await store.get("uid:1:fetch:videos")
    finally:
        await store.close()
