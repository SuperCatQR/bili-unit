"""TUI-facing service boundary over commands and observability snapshots."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ._db.paths import list_uids as _list_uids
from ._env import BiliSettings, get_settings
from ._types import CredentialProvider
from .command import BiliCommand, SyncCommandResult
from .fetching import CommandResult
from .observability import (
    DashboardSnapshot,
    RunSummary,
    UidDashboardSnapshot,
    load_dashboard_snapshot,
    load_run_summary,
    load_uid_dashboard_snapshot,
)
from .parsing import ParsingCommandResult
from .processing import ProcessingCommandResult


@dataclass
class BiliService:
    """Stable application-facing boundary for future TUI surfaces.

    ``BiliCommand`` remains the write-side API. This service adds read-side
    dashboard helpers beside those commands so UI code has one dependency.
    """

    command: BiliCommand
    settings: BiliSettings

    def list_uids(self) -> list[int]:
        """Return known uids under the configured DB root."""
        return _list_uids(self.settings.bili_db_dir)

    async def dashboard(
        self,
        *,
        uid: int | None = None,
        recent_limit: int = 20,
    ) -> DashboardSnapshot:
        """Load dashboard-ready snapshots for all uids or one uid."""
        return await load_dashboard_snapshot(
            root=self.settings.bili_db_dir,
            uid=uid,
            recent_limit=recent_limit,
        )

    async def inspect_uid(
        self,
        uid: int,
        *,
        recent_limit: int = 20,
    ) -> UidDashboardSnapshot:
        """Load one uid's current manifest, latest run, and recommendations."""
        return await load_uid_dashboard_snapshot(
            root=self.settings.bili_db_dir,
            uid=uid,
            recent_limit=recent_limit,
        )

    async def run_summary(
        self,
        uid: int,
        *,
        run_id: str | None = None,
        recent_limit: int = 20,
        filter_events_to_run: bool = True,
    ) -> RunSummary:
        """Load the detailed run summary used by CLI and future TUI panels."""
        return await load_run_summary(
            uid=uid,
            root=self.settings.bili_db_dir,
            run_id=run_id,
            recent_limit=recent_limit,
            filter_events_to_run=filter_events_to_run,
        )

    async def fetch(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        mode: str = "incremental",
    ) -> CommandResult:
        return await self.command.fetch(uid, endpoints=endpoints, mode=mode)

    async def parse(
        self,
        uid: int,
        mode: str = "full",
        models: list[str] | None = None,
        download_images: bool = False,
    ) -> ParsingCommandResult:
        return await self.command.parse(
            uid,
            mode=mode,
            models=models,
            download_images=download_images,
        )

    async def sync(
        self,
        uid: int,
        endpoints: list[str] | None = None,
        *,
        fetch_mode: str = "incremental",
        parse_mode: str = "incremental",
        parse_models: list[str] | None = None,
        download_images: bool = False,
    ) -> SyncCommandResult:
        return await self.command.sync(
            uid,
            endpoints=endpoints,
            fetch_mode=fetch_mode,
            parse_mode=parse_mode,
            parse_models=parse_models,
            download_images=download_images,
        )

    async def asr(
        self,
        uid: int,
        mode: str = "incremental",
        *,
        limit: int | None = None,
        only_bvids: list[str] | None = None,
        exclude_bvids: list[str] | None = None,
        retry_failed_only: bool = False,
        dry_run: bool = False,
        max_audio_seconds: float | None = None,
        max_audio_tokens: int | None = None,
    ) -> ProcessingCommandResult:
        return await self.command.asr(
            uid,
            mode=mode,
            limit=limit,
            only_bvids=only_bvids,
            exclude_bvids=exclude_bvids,
            retry_failed_only=retry_failed_only,
            dry_run=dry_run,
            max_audio_seconds=max_audio_seconds,
            max_audio_tokens=max_audio_tokens,
        )

    async def delete_uid(self, uid: int) -> dict[str, int]:
        return await self.command.delete_uid(uid)

    async def close(self) -> None:
        await self.command.close()


async def assemble_service(
    settings: BiliSettings | None = None,
    *,
    asr_backend_override: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> BiliService:
    """Assemble a TUI-facing service with write commands and read snapshots."""
    from . import assemble

    resolved = settings if settings is not None else get_settings()
    command = await assemble(
        resolved,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
    )
    return BiliService(command=command, settings=resolved)


@asynccontextmanager
async def service_session(
    settings: BiliSettings | None = None,
    *,
    asr_backend_override: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> AsyncIterator[BiliService]:
    """Context manager variant of :func:`assemble_service`."""
    service = await assemble_service(
        settings,
        asr_backend_override=asr_backend_override,
        credential_provider=credential_provider,
    )
    try:
        yield service
    finally:
        await service.close()


__all__ = ["BiliService", "assemble_service", "service_session"]
