"""Progress rendering seam shared by runners.

Core runners describe units of work and labels; this module owns the small
adapter contract that turns those units into terminal progress today, and a
different sink later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from ._logging import Progress


class ProgressHandle(Protocol):
    def update(self, n: int = 1, *, postfix: str | None = None) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> ProgressHandle: ...

    def __exit__(self, *exc: Any) -> None: ...


ProgressFactory = Callable[..., ProgressHandle]


def default_progress_factory(*, total: int, label: str, **kwargs: Any) -> Progress:
    return Progress(total=total, label=label, **kwargs)


async def gather_with_progress(
    coros: Sequence[Awaitable[Any]],
    *,
    total: int,
    label: str,
    progress_factory: ProgressFactory = default_progress_factory,
) -> list[Any | Exception]:
    """Run coroutines concurrently and tick progress once per completion.

    Exceptions are returned instead of raised to preserve the fetch runner's
    long-standing gather semantics: endpoint bodies record their own terminal
    failures and a sibling endpoint should not be cancelled by one exception.
    """
    with progress_factory(total=total, label=label) as progress:

        async def _wrap(coro: Awaitable[Any]) -> Any | Exception:
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 - preserve gather semantics
                return exc
            finally:
                progress.update(1)

        return await asyncio.gather(*[_wrap(coro) for coro in coros])


__all__ = [
    "ProgressFactory",
    "ProgressHandle",
    "default_progress_factory",
    "gather_with_progress",
]
