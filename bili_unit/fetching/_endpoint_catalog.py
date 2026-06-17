"""Endpoint metadata registry for fetching."""

from __future__ import annotations

from ._endpoint_groups import (
    channel_and_upower_endpoints,
    content_endpoints,
    user_endpoints,
    video_endpoints,
)
from ._endpoint_spec import EndpointSpec


def _build_endpoints() -> list[EndpointSpec]:
    return [
        *user_endpoints(),
        *video_endpoints(),
        *content_endpoints(),
        *channel_and_upower_endpoints(),
    ]


ENDPOINTS: list[EndpointSpec] = _build_endpoints()
ENDPOINT_BY_NAME: dict[str, EndpointSpec] = {ep.name: ep for ep in ENDPOINTS}


def get_endpoint(name: str) -> EndpointSpec | None:
    return ENDPOINT_BY_NAME.get(name)


# Profiles — 用于 CLI --profile 选择端点子集（issue #2）。
#   "all"      → None sentinel，所有已注册端点（向后兼容默认）
#   "parsing"  → parsing 层实际消费的 12 个端点（≈ 2-3 分钟 / 中等账号）
#   "minimal"  → 5 个 listing 端点，用于 smoke / CI
PARSING_PROFILE: frozenset[str] = frozenset({
    "user_info", "relation_info", "up_stat", "overview_stat",
    "articles", "article_detail", "article_list_detail",
    "opus", "opus_detail",
    "dynamics",
    "videos", "video_detail",
})
MINIMAL_PROFILE: frozenset[str] = frozenset({
    "user_info", "videos", "articles", "opus", "dynamics",
})
PROFILES: dict[str, frozenset[str] | None] = {
    "all": None,
    "parsing": PARSING_PROFILE,
    "minimal": MINIMAL_PROFILE,
}


def resolve_profile(name: str) -> list[str] | None:
    """Translate a profile name into a concrete endpoint list."""
    if name not in PROFILES:
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"unknown profile: {name!r} (known: {known})")
    members = PROFILES[name]
    if members is None:
        return None
    return [ep.name for ep in ENDPOINTS if ep.name in members]


__all__ = [
    "ENDPOINTS",
    "ENDPOINT_BY_NAME",
    "MINIMAL_PROFILE",
    "PARSING_PROFILE",
    "PROFILES",
    "get_endpoint",
    "resolve_profile",
]
