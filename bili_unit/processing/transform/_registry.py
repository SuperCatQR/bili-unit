# transform/_registry — handler discovery.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._base import TransformHandler


def _build_registry() -> dict[str, TransformHandler]:
    # Lazy import to avoid circular imports during module load.
    from . import articles, dynamics, video_metadata
    return {
        video_metadata.HANDLER.item_type: video_metadata.HANDLER,
        dynamics.HANDLER.item_type: dynamics.HANDLER,
        articles.HANDLER.item_type: articles.HANDLER,
    }


_REGISTRY: dict[str, TransformHandler] | None = None


def _registry() -> dict[str, TransformHandler]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


class _HandlersView:
    """Sorted view over registered handlers; iterable + len()."""

    def __iter__(self):
        return iter(_registry().values())

    def __len__(self):
        return len(_registry())

    def __contains__(self, item_type: str) -> bool:
        return item_type in _registry()

    def names(self) -> list[str]:
        return list(_registry().keys())


HANDLERS = _HandlersView()


def get_handler(item_type: str) -> TransformHandler | None:
    return _registry().get(item_type)
