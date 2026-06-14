# tests for ASRCacheStore — segment-keyed resume cache.

import json

import pytest

from bili_unit.processing.audio import (
    ASRCacheStore,
    ASRResult,
    CachedSegment,
    Mp3Segment,
)
from bili_unit.processing.audio._asr_cache import CACHE_VERSION, MATCH_TOLERANCE_S
from bili_unit.processing.runner._audio_work import audio_transcribe_page


def test_load_page_cold_returns_empty(tmp_path):
    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(1, "BV1", 0)
    assert page.uid == 1
    assert page.bvid == "BV1"
    assert page.page_index == 0
    assert page.segments == []


def test_upsert_persists_to_disk_atomically(tmp_path):
    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(42, "BVabc", 0)
    seg = CachedSegment(
        start_s=0.0, end_s=830.0, text="hello",
        language="zh", duration=830.0, model="m",
    )
    cache.upsert(page, seg)

    # File should exist with the expected schema.
    f = tmp_path / "42" / "BVabc" / "0.json"
    assert f.exists()
    raw = json.loads(f.read_text(encoding="utf-8"))
    assert raw["version"] == CACHE_VERSION
    assert raw["uid"] == 42
    assert raw["bvid"] == "BVabc"
    assert raw["page_index"] == 0
    assert len(raw["segments"]) == 1
    assert raw["segments"][0]["text"] == "hello"
    # No tmp file left over from atomic rename.
    assert not (tmp_path / "42" / "BVabc" / "0.json.tmp").exists()


def test_find_matches_within_tolerance(tmp_path):
    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(1, "BV1", 0)
    seg = CachedSegment(
        start_s=100.0, end_s=200.0, text="t",
        language="zh", duration=100.0, model="m",
    )
    cache.upsert(page, seg)

    # Reload from disk.
    page2 = cache.load_page(1, "BV1", 0)
    # Exact match.
    assert cache.find(page2, 100.0, 200.0) is not None
    # Within tolerance (well below MATCH_TOLERANCE_S).
    near = MATCH_TOLERANCE_S * 0.5
    assert cache.find(page2, 100.0 + near, 200.0 - near) is not None
    # Outside tolerance.
    far = MATCH_TOLERANCE_S * 2
    assert cache.find(page2, 100.0 + far, 200.0) is None


def test_upsert_replaces_within_tolerance(tmp_path):
    """Re-upsert with a near-identical range replaces (does not duplicate)."""
    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(1, "BV1", 0)
    cache.upsert(page, CachedSegment(
        start_s=0.0, end_s=100.0, text="v1",
        language="zh", duration=100.0, model="m",
    ))
    cache.upsert(page, CachedSegment(
        start_s=0.05, end_s=99.95, text="v2",
        language="zh", duration=100.0, model="m",
    ))

    page2 = cache.load_page(1, "BV1", 0)
    assert len(page2.segments) == 1
    assert page2.segments[0].text == "v2"


def test_upsert_keeps_distinct_segments(tmp_path):
    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(1, "BV1", 0)
    cache.upsert(page, CachedSegment(
        start_s=0.0, end_s=400.0, text="A",
        language="zh", duration=400.0, model="m",
    ))
    cache.upsert(page, CachedSegment(
        start_s=400.0, end_s=800.0, text="B",
        language="zh", duration=400.0, model="m",
    ))

    page2 = cache.load_page(1, "BV1", 0)
    assert len(page2.segments) == 2
    # Stored sorted by (start_s, end_s).
    assert [s.text for s in page2.segments] == ["A", "B"]


def test_clear_bvid_removes_all_pages(tmp_path):
    cache = ASRCacheStore(tmp_path)
    page0 = cache.load_page(7, "BVx", 0)
    page1 = cache.load_page(7, "BVx", 1)
    cache.upsert(page0, CachedSegment(0.0, 1.0, "p0", "zh", 1.0, "m"))
    cache.upsert(page1, CachedSegment(0.0, 1.0, "p1", "zh", 1.0, "m"))
    assert (tmp_path / "7" / "BVx" / "0.json").exists()
    assert (tmp_path / "7" / "BVx" / "1.json").exists()

    cache.clear_bvid(7, "BVx")

    assert not (tmp_path / "7" / "BVx" / "0.json").exists()
    assert not (tmp_path / "7" / "BVx" / "1.json").exists()
    # Directory itself should be gone too.
    assert not (tmp_path / "7" / "BVx").exists()


def test_clear_bvid_noop_when_missing(tmp_path):
    """Clearing a never-cached bvid must not error."""
    cache = ASRCacheStore(tmp_path)
    cache.clear_bvid(99, "BVnope")  # should not raise


def test_load_page_drops_corrupt_file(tmp_path):
    """A garbage JSON file must be treated as a cold cache (not crash)."""
    f = tmp_path / "1" / "BV1" / "0.json"
    f.parent.mkdir(parents=True)
    f.write_text("not valid json {{{", encoding="utf-8")

    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(1, "BV1", 0)
    assert page.segments == []  # treated as cold start


def test_load_page_drops_version_mismatch(tmp_path):
    """A future / past schema version must be ignored, not honoured."""
    f = tmp_path / "1" / "BV1" / "0.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({
        "version": CACHE_VERSION + 999,
        "segments": [{"start_s": 0.0, "end_s": 1.0, "text": "old"}],
    }), encoding="utf-8")

    cache = ASRCacheStore(tmp_path)
    page = cache.load_page(1, "BV1", 0)
    assert page.segments == []


# ---------- audio_transcribe_page returns segments list ---------------------


class _StubASRBackend:
    """Minimal ASR backend stub: pops a queue of ASRResult per call."""

    model = "stub-asr-v1"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def transcribe(self, audio_data, mime_type="audio/mp3", language="auto"):
        self.calls += 1
        return self._results.pop(0)

    async def close(self):
        return None


def _make_seg(tmp_path, name, start_s, end_s):
    p = tmp_path / name
    p.write_bytes(b"x")
    return Mp3Segment(p, start_s, end_s)


@pytest.mark.asyncio
async def test_transcribe_page_returns_segments_list(tmp_path):
    """A page with 3 mp3 segments returns 3 segments dicts in order."""
    cache = ASRCacheStore(tmp_path / "cache")
    seg_files = [
        _make_seg(tmp_path, "s0.mp3", 0.0, 300.0),
        _make_seg(tmp_path, "s1.mp3", 300.0, 600.0),
        _make_seg(tmp_path, "s2.mp3", 600.0, 900.0),
    ]
    backend = _StubASRBackend([
        ASRResult(text="alpha", duration=300.0, model="stub-asr-v1"),
        ASRResult(text="beta",  duration=300.0, model="stub-asr-v1"),
        ASRResult(text="gamma", duration=300.0, model="stub-asr-v1"),
    ])

    trans = await audio_transcribe_page(
        backend, cache, uid=1, bvid="BVseg", page_index=0,
        segments=seg_files, asr_language="zh",
    )

    segs = trans["segments"]
    assert len(segs) == 3
    assert backend.calls == 3

    # Order matches input order; per-entry fields fully populated.
    assert [s["start_s"] for s in segs] == [0.0, 300.0, 600.0]
    assert [s["end_s"] for s in segs] == [300.0, 600.0, 900.0]
    assert [s["text"] for s in segs] == ["alpha", "beta", "gamma"]
    for s in segs:
        assert s["duration"] == 300.0
        assert s["model"] == "stub-asr-v1"
        assert set(s.keys()) == {"start_s", "end_s", "text", "duration", "model"}

    # Stitched text + cache-hits accounting still untouched.
    assert trans["cache_hits"] == 0
    assert "alpha" in trans["text"] and "gamma" in trans["text"]


@pytest.mark.asyncio
async def test_transcribe_page_segments_filled_from_cache(tmp_path):
    """Second pass: every segment hits cache, segments still come back filled."""
    cache = ASRCacheStore(tmp_path / "cache")
    seg_files = [
        _make_seg(tmp_path, "s0.mp3", 0.0, 300.0),
        _make_seg(tmp_path, "s1.mp3", 300.0, 600.0),
        _make_seg(tmp_path, "s2.mp3", 600.0, 900.0),
    ]

    backend1 = _StubASRBackend([
        ASRResult(text="A", duration=300.0, model="stub-asr-v1"),
        ASRResult(text="B", duration=300.0, model="stub-asr-v1"),
        ASRResult(text="C", duration=300.0, model="stub-asr-v1"),
    ])
    first = await audio_transcribe_page(
        backend1, cache, uid=2, bvid="BVcacheFill", page_index=0,
        segments=seg_files, asr_language="zh",
    )
    assert backend1.calls == 3
    assert first["cache_hits"] == 0

    # Second run with empty backend queue — all hits must come from cache.
    backend2 = _StubASRBackend([])
    second = await audio_transcribe_page(
        backend2, cache, uid=2, bvid="BVcacheFill", page_index=0,
        segments=seg_files, asr_language="zh",
    )

    assert backend2.calls == 0
    assert second["cache_hits"] == 3
    segs = second["segments"]
    assert len(segs) == 3
    assert [s["text"] for s in segs] == ["A", "B", "C"]
    assert [s["start_s"] for s in segs] == [0.0, 300.0, 600.0]
    assert [s["end_s"] for s in segs] == [300.0, 600.0, 900.0]
    for s in segs:
        assert s["duration"] == 300.0
        assert s["model"] == "stub-asr-v1"


@pytest.mark.asyncio
async def test_transcribe_page_segments_mixed_cache_and_fresh(tmp_path):
    """Two cache hits + one fresh ASR call still yields ordered 3-entry list."""
    cache = ASRCacheStore(tmp_path / "cache")
    page = cache.load_page(3, "BVmix", 0)
    cache.upsert(page, CachedSegment(
        start_s=0.0, end_s=300.0, text="cached-1",
        language="zh", duration=300.0, model="stub-asr-v1",
    ))
    cache.upsert(page, CachedSegment(
        start_s=300.0, end_s=600.0, text="cached-2",
        language="zh", duration=300.0, model="stub-asr-v1",
    ))

    seg_files = [
        _make_seg(tmp_path, "s0.mp3", 0.0, 300.0),
        _make_seg(tmp_path, "s1.mp3", 300.0, 600.0),
        _make_seg(tmp_path, "s2.mp3", 600.0, 900.0),
    ]
    backend = _StubASRBackend([
        ASRResult(text="fresh-3", duration=300.0, model="stub-asr-v1"),
    ])

    trans = await audio_transcribe_page(
        backend, cache, uid=3, bvid="BVmix", page_index=0,
        segments=seg_files, asr_language="zh",
    )

    assert backend.calls == 1
    assert trans["cache_hits"] == 2
    segs = trans["segments"]
    assert len(segs) == 3
    # Order is the input order, not cache order.
    assert [s["text"] for s in segs] == ["cached-1", "cached-2", "fresh-3"]
    assert [s["start_s"] for s in segs] == [0.0, 300.0, 600.0]
    assert [s["end_s"] for s in segs] == [300.0, 600.0, 900.0]
    for s in segs:
        assert s["model"] == "stub-asr-v1"
        assert s["duration"] == 300.0
