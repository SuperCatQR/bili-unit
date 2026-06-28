# tests for bili_unit/fetching/_endpoint_catalog — endpoint registry correctness.
# Run: uv run pytest bili_unit/tests/test_endpoint_catalog.py -v

from __future__ import annotations

import pytest

from bili_unit.fetching._endpoint_catalog import (
    ENDPOINTS,
    ENDPOINT_BY_NAME,
    MINIMAL_PROFILE,
    PROFILES,
    get_endpoint,
    resolve_profile,
)
from bili_unit.fetching._endpoint_spec import EndpointSpec


# ---------------------------------------------------------------------------
# Endpoint count
# ---------------------------------------------------------------------------


def test_endpoint_count() -> None:
    """Catalog has exactly 63 endpoints: 33 uid-level + 30 item-level."""
    assert len(ENDPOINTS) == 63

    uid_level = [ep for ep in ENDPOINTS if ep.kind == "uid"]
    item_level = [ep for ep in ENDPOINTS if ep.kind == "item"]
    assert len(uid_level) == 33
    assert len(item_level) == 30


def test_all_endpoints_have_unique_names() -> None:
    """No two endpoints share the same name."""
    names = [ep.name for ep in ENDPOINTS]
    assert len(names) == len(set(names))


def test_endpoint_by_name_covers_all() -> None:
    """ENDPOINT_BY_NAME has the same entries as ENDPOINTS."""
    assert len(ENDPOINT_BY_NAME) == len(ENDPOINTS)
    for ep in ENDPOINTS:
        assert ENDPOINT_BY_NAME[ep.name] is ep


# ---------------------------------------------------------------------------
# EndpointSpec field validation
# ---------------------------------------------------------------------------


def test_every_endpoint_has_rate_limit_key() -> None:
    """Every EndpointSpec has a non-empty rate_limit_key."""
    missing = [ep.name for ep in ENDPOINTS if not ep.rate_limit_key]
    assert not missing, f"Endpoints missing rate_limit_key: {missing}"


def test_every_endpoint_has_pagination_strategy() -> None:
    """Every EndpointSpec has a pagination_strategy set (not just the default 'none' is fine)."""
    # All endpoints must have a pagination_strategy — even "none" is valid,
    # but it must be explicitly set or defaulted.
    for ep in ENDPOINTS:
        assert ep.pagination_strategy in (
            "none", "page", "cursor", "anchor", "legacy_offset", "oid", "custom",
        ), f"{ep.name}: unknown pagination_strategy {ep.pagination_strategy!r}"


def test_item_level_endpoints_have_source_or_item_id_path() -> None:
    """Every item-level endpoint has source_endpoint+extract_items or item_id_path(s)."""
    item_eps = [ep for ep in ENDPOINTS if ep.kind == "item"]
    missing = [
        ep.name for ep in item_eps
        if ep.item_id_path is None
        and ep.item_id_paths is None
        and ep.source_endpoint is None
    ]
    assert not missing, f"Item-level endpoints with no item_id_path(s) or source_endpoint: {missing}"


def test_uid_level_endpoints_are_kind_uid() -> None:
    """All 33 uid-level endpoints are correctly marked as kind='uid'."""
    uid_eps = [ep for ep in ENDPOINTS if ep.kind == "uid"]
    assert len(uid_eps) == 33
    for ep in uid_eps:
        assert ep.kind == "uid", f"{ep.name}: expected kind='uid'"


# ---------------------------------------------------------------------------
# get_endpoint
# ---------------------------------------------------------------------------


def test_get_endpoint_known() -> None:
    """get_endpoint returns the correct spec for a known endpoint."""
    ep = get_endpoint("user_info")
    assert ep is not None
    assert isinstance(ep, EndpointSpec)
    assert ep.name == "user_info"


def test_get_endpoint_unknown() -> None:
    """get_endpoint returns None for an unknown endpoint."""
    assert get_endpoint("nonexistent_endpoint") is None


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_profiles_all_is_none() -> None:
    """The 'all' profile is the None sentinel."""
    assert PROFILES["all"] is None


def test_minimal_profile_has_five_endpoints() -> None:
    """The 'minimal' profile has exactly 5 listing endpoints."""
    assert len(MINIMAL_PROFILE) == 5
    assert MINIMAL_PROFILE == {"user_info", "videos", "articles", "opus", "dynamics"}


def test_minimal_profile_members_are_registered() -> None:
    """Every endpoint in the minimal profile is in the catalog."""
    for name in MINIMAL_PROFILE:
        assert get_endpoint(name) is not None, f"{name} not in catalog"


# ---------------------------------------------------------------------------
# resolve_profile
# ---------------------------------------------------------------------------


def test_resolve_profile_all_returns_none() -> None:
    """resolve_profile('all') returns None (meaning all endpoints)."""
    assert resolve_profile("all") is None


def test_resolve_profile_minimal_returns_five_names() -> None:
    """resolve_profile('minimal') returns exactly the 5 minimal endpoint names."""
    names = resolve_profile("minimal")
    assert names is not None
    assert len(names) == 5
    assert set(names) == set(MINIMAL_PROFILE)


def test_resolve_profile_unknown_raises_keyerror() -> None:
    """resolve_profile with an unknown name raises KeyError."""
    with pytest.raises(KeyError, match="unknown profile"):
        resolve_profile("bogus")


def test_resolve_profile_keyerror_message_lists_known() -> None:
    """The KeyError message includes the list of known profiles."""
    with pytest.raises(KeyError, match="all"):
        resolve_profile("bogus")


# ---------------------------------------------------------------------------
# ENDPOINTS immutability smoke test
# ---------------------------------------------------------------------------


def test_endpoints_is_list_of_endpoint_specs() -> None:
    """ENDPOINTS is a list of EndpointSpec instances."""
    assert isinstance(ENDPOINTS, list)
    for ep in ENDPOINTS:
        assert isinstance(ep, EndpointSpec), f"Expected EndpointSpec, got {type(ep)}"
