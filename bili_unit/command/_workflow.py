"""Write-side workflow modules shared by CLI and top-level command helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..fetching import CommandResult, TaskStatus

if TYPE_CHECKING:
    from ..parsing import ParsingCommandResult
    from . import BiliCommand


@dataclass
class SyncCommandResult:
    """Result for the public ``sync`` workflow.

    ``sync`` is a convenience workflow over two existing stages. Stage state is
    still recorded separately in ``stage_task[fetching]`` and
    ``stage_task[parsing]``.
    """

    uid: int
    status: str
    fetch: CommandResult
    parse: ParsingCommandResult | None


async def sync_uid(
    command: BiliCommand,
    uid: int,
    endpoints: list[str] | None = None,
    *,
    fetch_mode: str = "incremental",
    parse_mode: str = "full",
    download_images: bool = False,
) -> SyncCommandResult:
    """Run the common write-side workflow: fetching followed by parsing.

    Parsing still runs after a PARTIAL fetch so successful raw payloads are
    materialized. Hard fetching failures skip parsing.
    """
    fetch_result = await command.fetch(uid, endpoints=endpoints, mode=fetch_mode)
    if fetch_result.status in {
        TaskStatus.FAILED_RETRYABLE,
        TaskStatus.FAILED_EXHAUSTED,
        TaskStatus.FAILED_PERMANENT,
    }:
        return SyncCommandResult(
            uid=uid,
            status=fetch_result.status.value,
            fetch=fetch_result,
            parse=None,
        )

    parse_result = await command.parse(
        uid,
        mode=parse_mode,
        download_images=download_images,
    )
    status = "SUCCESS"
    if (
        fetch_result.status != TaskStatus.SUCCESS
        or parse_result.status.value != "SUCCESS"
    ):
        status = "PARTIAL"
    return SyncCommandResult(
        uid=uid,
        status=status,
        fetch=fetch_result,
        parse=parse_result,
    )


__all__ = ["SyncCommandResult", "sync_uid"]
