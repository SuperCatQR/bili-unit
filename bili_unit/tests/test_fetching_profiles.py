# Tests for the PROFILES preset and resolve_profile() helper (issue #2).

import pytest

from bili_unit.fetching.client import ENDPOINTS, PROFILES, resolve_profile

ALL_REGISTERED = {ep.name for ep in ENDPOINTS}


def test_profiles_keys():
    assert set(PROFILES) == {"all", "parsing", "minimal"}


def test_all_profile_is_none_sentinel():
    assert PROFILES["all"] is None


def test_parsing_profile_members_are_registered():
    members = PROFILES["parsing"]
    assert members is not None
    assert members <= ALL_REGISTERED, members - ALL_REGISTERED


def test_minimal_profile_members_are_registered():
    members = PROFILES["minimal"]
    assert members is not None
    assert members <= ALL_REGISTERED, members - ALL_REGISTERED


def test_minimal_subset_of_parsing():
    """minimal 是 parsing 的真子集 — minimal 跑完后再升级到 parsing 不丢端点。"""
    assert PROFILES["minimal"] < PROFILES["parsing"]


def test_parsing_covers_known_consumed_endpoints():
    # 与 bili_unit/parsing/specs.py 的 source_endpoints 对齐。
    expected = {
        "user_info", "relation_info", "up_stat", "overview_stat",
        "articles", "article_detail", "article_list_detail",
        "opus", "opus_detail",
        "dynamics",
        "videos", "video_detail",
    }
    assert PROFILES["parsing"] == frozenset(expected)


def test_resolve_all_returns_none():
    assert resolve_profile("all") is None


def test_resolve_parsing_returns_ordered_endpoint_list():
    result = resolve_profile("parsing")
    assert result is not None
    assert set(result) == set(PROFILES["parsing"])
    # 顺序与 ENDPOINTS 注册顺序一致（便于 progress bar 稳定）
    registered_order = [ep.name for ep in ENDPOINTS]
    assert result == [n for n in registered_order if n in PROFILES["parsing"]]


def test_resolve_minimal_returns_endpoint_list():
    result = resolve_profile("minimal")
    assert result is not None
    assert set(result) == set(PROFILES["minimal"])


def test_resolve_unknown_raises():
    with pytest.raises(KeyError):
        resolve_profile("does-not-exist")
