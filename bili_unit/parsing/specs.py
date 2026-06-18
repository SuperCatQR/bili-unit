"""Registry for parsing models and their materialization handlers."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsingSpec:
    """Static configuration for one parsing model.

    ``required_endpoints`` means the minimum raw inputs needed to materialize
    a row. It is intentionally distinct from a model's ``is_complete`` rule:
    list-only article/opus rows can be useful even when detail endpoints are
    missing.
    """

    name: str
    materializer_handler: str
    model: str | None = None
    source_endpoints: tuple[str, ...] = ()
    required_endpoints: tuple[str, ...] = ()
    priority: int = 0
    singleton: bool = False

    def parser_cls(self) -> type[Any]:
        """Return the typed parser/model class for this parsing model."""
        from .models import get_parser

        return get_parser(self.model or self.name)

    def default_item_id(self, uid: int) -> str:
        """Return the uid-derived item id for singleton models."""
        if not self.singleton:
            raise ValueError(f"{self.name} does not have a uid-derived item id")
        return str(uid)


PARSING_SPECS: tuple[ParsingSpec, ...] = (
    ParsingSpec(
        name="user_profile",
        model="user_profile",
        materializer_handler="_parse_user_profile",
        source_endpoints=("user_info", "relation_info", "up_stat", "overview_stat"),
        required_endpoints=("user_info", "relation_info", "up_stat"),
        priority=10,
        singleton=True,
    ),
    ParsingSpec(
        name="video_work",
        model="video_work",
        materializer_handler="_parse_video_work",
        source_endpoints=("video_detail",),
        required_endpoints=("video_detail",),
        priority=20,
    ),
    ParsingSpec(
        name="video_subtitle",
        model="video_subtitle",
        materializer_handler="_parse_video_subtitle",
        source_endpoints=("video_subtitle",),
        required_endpoints=("video_subtitle",),
        priority=25,
    ),
    ParsingSpec(
        name="article_post",
        model="article_post",
        materializer_handler="_parse_article_posts",
        source_endpoints=("articles", "article_detail", "article_list_detail"),
        required_endpoints=("articles",),
        priority=30,
    ),
    ParsingSpec(
        name="opus_post",
        model="opus_post",
        materializer_handler="_parse_opus_posts",
        source_endpoints=("opus", "opus_detail"),
        required_endpoints=("opus",),
        priority=40,
    ),
    ParsingSpec(
        name="dynamic_event",
        model="dynamic_event",
        materializer_handler="_parse_dynamic_events",
        source_endpoints=("dynamics",),
        required_endpoints=("dynamics",),
        priority=50,
    ),
)

PARSING_SPEC_REGISTRY = MappingProxyType({spec.name: spec for spec in PARSING_SPECS})
MODEL_ORDER: tuple[str, ...] = tuple(spec.name for spec in PARSING_SPECS)


def get_spec(model_name: str) -> ParsingSpec:
    """Return the parsing spec for a model name."""
    try:
        return PARSING_SPEC_REGISTRY[model_name]
    except KeyError:
        raise KeyError(model_name) from None


def iter_specs() -> tuple[ParsingSpec, ...]:
    """Return parsing specs in parse_uid order."""
    return PARSING_SPECS
