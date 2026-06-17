"""Top-level ``session()`` async context manager behavior tests.

The CLI uses the same lifecycle helper: assemble one ``BiliCommand`` and make
sure it is closed on both normal and exceptional exits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bili_unit
from bili_unit import BiliSettings


def _make_settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(
        bili_db_dir=str(tmp_path / "db"),
        bili_processing_temp_dir=str(tmp_path / "processing_temp"),
        bili_processing_asr_cache_dir=str(tmp_path / "asr_cache"),
        bili_processing_asr_backend="mock",
    )


async def test_session_normal_path_calls_close(tmp_path: Path) -> None:
    """Normal exit invokes ``cmd.close()``."""
    settings = _make_settings(tmp_path)
    closed = {"flag": False}

    async with bili_unit.session(settings=settings) as cmd:
        assert isinstance(cmd, bili_unit.BiliCommand)
        original_close = cmd.close

        async def spy_close() -> None:
            closed["flag"] = True
            await original_close()

        cmd.close = spy_close  # type: ignore[method-assign]

    assert closed["flag"] is True


async def test_session_exception_path_still_calls_close(tmp_path: Path) -> None:
    """Exception path still drives ``close()``."""
    settings = _make_settings(tmp_path)
    closed = {"flag": False}

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with bili_unit.session(settings=settings) as cmd:
            original_close = cmd.close

            async def spy_close() -> None:
                closed["flag"] = True
                await original_close()

            cmd.close = spy_close  # type: ignore[method-assign]
            raise _Boom("user code went wrong")

    assert closed["flag"] is True


async def test_session_yields_single_command(tmp_path: Path) -> None:
    """session() yields a single ``BiliCommand``."""
    settings = _make_settings(tmp_path)
    async with bili_unit.session(settings=settings) as cmd:
        assert isinstance(cmd, bili_unit.BiliCommand)
        assert hasattr(cmd, "sync")
        assert hasattr(cmd, "fetch")
        assert hasattr(cmd, "parse")
        assert hasattr(cmd, "process")
        assert hasattr(cmd, "delete_uid")
