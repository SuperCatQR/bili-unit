"""Top-level ``assemble()`` settings / credential injection tests."""
from __future__ import annotations

from pathlib import Path

import bili_unit
from bili_unit import BiliSettings


def _make_settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "processing_temp"),
        bili_processing_asr_cache_dir=str(tmp_path / "asr_cache"),
        bili_processing_asr_backend="mock",
    )


async def test_assemble_with_explicit_settings(tmp_path: Path) -> None:
    """The explicit BiliSettings flows through to the unified command."""
    settings = _make_settings(tmp_path)
    cmd = await bili_unit.assemble(settings=settings)
    try:
        assert cmd._settings is settings
        assert bili_unit.db_path(42, settings).parent == Path(settings.bili_db_dir)
    finally:
        await cmd.close()


async def test_assemble_with_credential_provider(tmp_path: Path) -> None:
    """The injected ``credential_provider`` reaches ProcessingCommand."""
    settings = _make_settings(tmp_path)

    sentinel = object()

    async def fake_provider():  # noqa: ANN202 - test helper
        return sentinel

    cmd = await bili_unit.assemble(
        settings=settings,
        credential_provider=fake_provider,
    )
    try:
        proc_cmd = cmd._processing
        assert proc_cmd is not None
        assert proc_cmd._credential_provider is fake_provider
        assert proc_cmd._runner._credential_provider is fake_provider
    finally:
        await cmd.close()


async def test_assemble_default_path_unchanged(tmp_path: Path, monkeypatch) -> None:
    """``assemble()`` with no args lazy-loads from .env for CLI compatibility."""
    monkeypatch.setenv("BILI_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv(
        "BILI_PROCESSING_TEMP_DIR", str(tmp_path / "processing_temp"),
    )
    monkeypatch.setenv(
        "BILI_PROCESSING_ASR_CACHE_DIR", str(tmp_path / "asr_cache"),
    )
    monkeypatch.setenv("BILI_PROCESSING_ASR_BACKEND", "mock")
    bili_unit.reload_settings()

    try:
        cmd = await bili_unit.assemble()
        try:
            assert cmd._settings is not None
            assert cmd._settings.bili_db_dir == str(tmp_path / "db")
            assert cmd._settings.bili_processing_asr_backend == "mock"
        finally:
            await cmd.close()
    finally:
        bili_unit.reload_settings()
