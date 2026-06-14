"""SDK ``session()`` async context manager 行为测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

import bili_unit
from bili_unit import BiliSettings


def _make_settings(tmp_path: Path) -> BiliSettings:
    return BiliSettings(
        bili_fetching_data_dir=str(tmp_path / "fetching" / "data"),
        bili_fetching_error_dir=str(tmp_path / "fetching" / "error"),
        bili_parsing_data_dir=str(tmp_path / "parsing"),
        bili_processing_data_dir=str(tmp_path / "processing" / "data"),
        bili_processing_error_dir=str(tmp_path / "processing" / "error"),
        bili_processing_temp_dir=str(tmp_path / "processing" / "temp"),
        bili_processing_asr_backend="mock",
        bili_processing_asr_cache_dir=str(tmp_path / "asr_cache"),
    )


@pytest.mark.asyncio
async def test_session_normal_path_calls_close(tmp_path: Path) -> None:
    """正常退出 ctx 时 cmd.close() 被调用（store 关闭即视为关闭成功）。"""
    settings = _make_settings(tmp_path)
    closed = {"flag": False}

    async with bili_unit.session(settings=settings) as (cmd, qry):
        # patch close 来观察调用——保留原 close 的真实清理行为
        original_close = cmd.close

        async def spy_close():
            closed["flag"] = True
            await original_close()

        cmd.close = spy_close  # type: ignore[method-assign]
        # 在 ctx 内做点真实 query 看 cmd 仍然 usable
        tasks = await qry.fetching.list_tasks()
        assert tasks == []  # 空 store

    assert closed["flag"] is True


@pytest.mark.asyncio
async def test_session_exception_path_still_calls_close(tmp_path: Path) -> None:
    """异常路径下 close 仍然被调用——这是 ctx mgr 的核心保证。"""
    settings = _make_settings(tmp_path)
    closed = {"flag": False}

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with bili_unit.session(settings=settings) as (cmd, qry):
            original_close = cmd.close

            async def spy_close():
                closed["flag"] = True
                await original_close()

            cmd.close = spy_close  # type: ignore[method-assign]
            raise _Boom("user code went wrong")

    assert closed["flag"] is True


@pytest.mark.asyncio
async def test_session_yields_command_and_query(tmp_path: Path) -> None:
    """session() yield 出来的两个对象类型正确。"""
    settings = _make_settings(tmp_path)
    async with bili_unit.session(settings=settings) as (cmd, qry):
        assert isinstance(cmd, bili_unit.BiliCommand)
        assert isinstance(qry, bili_unit.BiliQuery)
