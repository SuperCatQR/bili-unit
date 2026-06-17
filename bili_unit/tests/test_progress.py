from __future__ import annotations

from bili_unit._progress import gather_with_progress


class _NullProgress:
    def __init__(self) -> None:
        self.updates: list[int] = []
        self.closed = False

    def __enter__(self) -> _NullProgress:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def update(self, n: int = 1, *, postfix: str | None = None) -> None:
        self.updates.append(n)

    def close(self) -> None:
        self.closed = True


async def test_gather_with_progress_ticks_and_preserves_exceptions() -> None:
    progress = _NullProgress()
    calls: list[tuple[int, str]] = []

    def progress_factory(*, total: int, label: str) -> _NullProgress:
        calls.append((total, label))
        return progress

    async def ok() -> str:
        return "ok"

    async def boom() -> str:
        raise RuntimeError("boom")

    results = await gather_with_progress(
        [ok(), boom()],
        total=2,
        label="fetch uid=1 endpoints",
        progress_factory=progress_factory,
    )

    assert calls == [(2, "fetch uid=1 endpoints")]
    assert results[0] == "ok"
    assert isinstance(results[1], RuntimeError)
    assert progress.updates == [1, 1]
    assert progress.closed is True
