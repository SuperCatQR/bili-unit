# fetching/protocols — read-side contract for downstream stages.
#
# parsing 与 processing 通过此 Protocol 间接依赖 fetching 的读侧接口；
# 不再依赖 :class:`bili_unit.fetching.query.Query` 这个具体类。这把
# docs/structure/bili.md §6 §8 中"fetching.query 单向衔接"的声明从注释
# 升级为可被 mypy/pyright 静态校验的不变量。
#
# 数据形状契约（EndpointDTO / EndpointStatus）继续住在
# :mod:`bili_unit.fetching` 顶层；本文件只描述行为接口。

from __future__ import annotations

from typing import Protocol

from . import EndpointDTO, EndpointStatus


class FetchingReadView(Protocol):
    """Read-side contract that downstream stages depend on.

    Implemented structurally by :class:`bili_unit.fetching.query.Query`
    (no nominal inheritance required). Tests / fakes can implement just
    these four methods to substitute fetching.
    """

    async def get_endpoint(
        self, uid: int, endpoint: str,
    ) -> EndpointDTO | None: ...

    async def list_video_details(
        self, uid: int,
    ) -> list[tuple[str, EndpointStatus]]: ...

    async def get_video_detail(
        self, uid: int, bvid: str,
    ) -> EndpointDTO | None: ...

    async def list_fanout_payloads(
        self, uid: int, endpoint: str,
    ) -> dict[str, dict]: ...


__all__ = ["FetchingReadView"]
