from __future__ import annotations

from pathlib import Path

import pytest

from bili_unit.parsing.data import ParsingDataStore, ParsingKeyMapper

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _strip(v: dict) -> dict:
    """Return a copy without the auto-injected updated_at field."""
    return {k: val for k, val in v.items() if k != "updated_at"}


# ---------------------------------------------------------------------------
# ParsingKeyMapper tests
# ---------------------------------------------------------------------------


class TestParsingKeyMapper:
    def setup_method(self) -> None:
        self.mapper = ParsingKeyMapper()
        self.base = Path("/data")

    # to_path ---------------------------------------------------------------

    def test_to_path_task(self):
        p = self.mapper.to_path(self.base, "uid:12345:task")
        assert p == self.base / "12345" / "task.json"

    def test_to_path_item(self):
        p = self.mapper.to_path(self.base, "uid:12345:parse:video_detail:BV123")
        assert p == self.base / "12345" / "video_detail" / "BV123.json"

    def test_to_path_malformed(self):
        p = self.mapper.to_path(self.base, "bogus_key")
        assert p.parent == self.base / "_misc"
        assert p.name == "bogus_key.json"

    # to_key ----------------------------------------------------------------

    def test_to_key_task(self):
        path = self.base / "12345" / "task.json"
        assert self.mapper.to_key(self.base, path) == "uid:12345:task"

    def test_to_key_item(self):
        path = self.base / "12345" / "video_detail" / "BV123.json"
        assert self.mapper.to_key(self.base, path) == "uid:12345:parse:video_detail:BV123"

    # prefix_to_scan_dir ----------------------------------------------------

    def test_prefix_to_scan_uid(self):
        p = self.mapper.prefix_to_scan_dir(self.base, "uid:12345")
        assert p == self.base / "12345"

    def test_prefix_to_scan_model(self):
        p = self.mapper.prefix_to_scan_dir(self.base, "uid:12345:parse:video_detail")
        assert p == self.base / "12345" / "video_detail"


# ---------------------------------------------------------------------------
# ParsingDataStore — CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_store_crud(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "parsing_data")
    await ds.open()
    try:
        # put + get
        await ds.put("uid:1:task", {"uid": 1, "status": "PENDING"})
        v = await ds.get("uid:1:task")
        assert v is not None
        assert _strip(v) == {"uid": 1, "status": "PENDING"}
        assert "updated_at" in v

        # overwrite
        await ds.put("uid:1:task", {"uid": 1, "status": "SUCCESS"})
        v = await ds.get("uid:1:task")
        assert _strip(v) == {"uid": 1, "status": "SUCCESS"}

        # delete
        await ds.delete("uid:1:task")
        assert await ds.get("uid:1:task") is None

        # list_prefix
        await ds.put("uid:10:parse:video_detail:BV001", {"title": "a"})
        await ds.put("uid:10:parse:video_detail:BV002", {"title": "b"})
        await ds.put("uid:20:parse:video_detail:BV003", {"title": "c"})
        items = await ds.list_prefix("uid:10:")
        assert len(items) == 2
        keys = {k for k, _ in items}
        assert "uid:10:parse:video_detail:BV001" in keys
        assert "uid:10:parse:video_detail:BV002" in keys
    finally:
        await ds.close()


@pytest.mark.asyncio
async def test_data_store_missing_key(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "parsing_data")
    await ds.open()
    try:
        assert await ds.get("uid:999:task") is None
    finally:
        await ds.close()


# ---------------------------------------------------------------------------
# ParsingDataStore — atomic helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_task_model_status(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "parsing_data")
    await ds.open()
    try:
        task_key = "uid:100:task"
        await ds.put(task_key, {
            "uid": 100,
            "status": "RUNNING",
            "models": {
                "video_detail": {"status": "PENDING", "count": 0},
            },
        })

        await ds.update_task_model_status(task_key, "video_detail", "SUCCESS", count=42)

        v = await ds.get(task_key)
        assert v is not None
        assert v["models"]["video_detail"]["status"] == "SUCCESS"
        assert v["models"]["video_detail"]["count"] == 42
    finally:
        await ds.close()


@pytest.mark.asyncio
async def test_update_task_model_status_creates_entry(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "parsing_data")
    await ds.open()
    try:
        task_key = "uid:200:task"
        await ds.put(task_key, {"uid": 200, "status": "RUNNING"})

        await ds.update_task_model_status(task_key, "opus", "RUNNING", count=5)

        v = await ds.get(task_key)
        assert v is not None
        assert "opus" in v["models"]
        assert v["models"]["opus"]["status"] == "RUNNING"
        assert v["models"]["opus"]["count"] == 5
    finally:
        await ds.close()


@pytest.mark.asyncio
async def test_update_task_images(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "parsing_data")
    await ds.open()
    try:
        task_key = "uid:300:task"
        await ds.put(task_key, {"uid": 300, "status": "RUNNING"})

        images_summary = {"total": 10, "ok": 8, "skipped": 1, "failed": 1, "failed_urls": ["http://x"]}
        await ds.update_task_images(task_key, images_summary)

        v = await ds.get(task_key)
        assert v is not None
        assert v["images"] == images_summary
    finally:
        await ds.close()


@pytest.mark.asyncio
async def test_update_task_images_missing_task(tmp_path: Path):
    ds = ParsingDataStore(tmp_path / "parsing_data")
    await ds.open()
    try:
        task_key = "uid:400:task"
        # Should be a no-op when task doesn't exist (mutator returns None).
        await ds.update_task_images(task_key, {"total": 0})
        assert await ds.get(task_key) is None
    finally:
        await ds.close()
