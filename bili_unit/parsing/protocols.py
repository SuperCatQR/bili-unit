# parsing/protocols — read-side contract for downstream stages.
#
# processing 通过此 Protocol 间接依赖 parsing 的读侧接口；不再依赖
# :class:`bili_unit.parsing.query.ParsingQuery` 这个具体类。这把
# docs/structure/bili.md 里"parsing.query 单向衔接"的声明从注释升级
# 为可被 mypy/pyright 静态校验的不变量。

from __future__ import annotations

from typing import Any, Protocol


class ParsingReadView(Protocol):
    """Read-side contract that downstream stages depend on.

    Implemented structurally by :class:`bili_unit.parsing.query.ParsingQuery`
    (no nominal inheritance required). Tests / fakes can implement just
    these two methods to substitute parsing.
    """

    async def get_video_detail(
        self, uid: int, bvid: str,
    ) -> dict[str, Any] | None: ...

    async def list_video_details(
        self, uid: int,
    ) -> list[dict[str, Any]]: ...


__all__ = ["ParsingReadView"]
