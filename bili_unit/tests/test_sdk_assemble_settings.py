"""SDK 入参化 assemble() 测试。

验证：
1. ``assemble(settings=...)`` 接收外部构造的 BiliSettings；
2. 三个 stage 的 store 路径都按 settings 中的 dir 字段创建在 tmp_path 下；
3. ``assemble(credential_provider=...)`` 注入的 provider 被传给 processing.command。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bili_unit
from bili_unit import BiliSettings


def _make_settings(tmp_path: Path) -> BiliSettings:
    """Construct a BiliSettings pointing at tmp_path subdirs (no .env required)."""
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
async def test_assemble_with_explicit_settings(tmp_path: Path) -> None:
    """assemble(settings=...) 让三 stage 用上 tmp_path 下的目录。"""
    settings = _make_settings(tmp_path)

    cmd, qry = await bili_unit.assemble(settings=settings)
    try:
        # tmp_path 下应当出现各 stage 的 store 目录（由 store.open() 创建）
        assert (tmp_path / "fetching" / "data").exists()
        assert (tmp_path / "fetching" / "error").exists()
        assert (tmp_path / "parsing").exists()
        assert (tmp_path / "processing" / "data").exists()
        assert (tmp_path / "processing" / "error").exists()
    finally:
        await cmd.close()


@pytest.mark.asyncio
async def test_assemble_with_credential_provider(tmp_path: Path) -> None:
    """注入的 credential_provider 被采用。"""
    settings = _make_settings(tmp_path)

    sentinel = object()

    async def fake_provider():
        return sentinel

    cmd, qry = await bili_unit.assemble(
        settings=settings,
        credential_provider=fake_provider,
    )
    try:
        # ProcessingCommand 把 credential_provider 传给了 ProcessingRunner，
        # ProcessingRunner 存为 self._credential_provider
        proc_cmd = cmd._processing
        assert proc_cmd is not None
        assert proc_cmd._runner._credential_provider is fake_provider
    finally:
        await cmd.close()


@pytest.mark.asyncio
async def test_assemble_default_path_unchanged(tmp_path: Path, monkeypatch) -> None:
    """assemble() 不传任何参数时仍走 .env lazy 加载——保留 CLI 兼容。"""
    # 用 monkeypatch 把 BILI_*_DIR 都指向 tmp_path，避免污染真实 cwd
    monkeypatch.setenv("BILI_FETCHING_DATA_DIR", str(tmp_path / "f_data"))
    monkeypatch.setenv("BILI_FETCHING_ERROR_DIR", str(tmp_path / "f_error"))
    monkeypatch.setenv("BILI_PARSING_DATA_DIR", str(tmp_path / "p_data"))
    monkeypatch.setenv("BILI_PROCESSING_DATA_DIR", str(tmp_path / "proc_data"))
    monkeypatch.setenv("BILI_PROCESSING_ERROR_DIR", str(tmp_path / "proc_error"))
    monkeypatch.setenv("BILI_PROCESSING_TEMP_DIR", str(tmp_path / "proc_temp"))
    monkeypatch.setenv("BILI_PROCESSING_ASR_BACKEND", "mock")
    bili_unit.reload_settings()  # 让单例重新读

    try:
        cmd, qry = await bili_unit.assemble()
        try:
            assert (tmp_path / "f_data").exists()
            assert (tmp_path / "proc_data").exists()
        finally:
            await cmd.close()
    finally:
        # 恢复全局 settings 状态
        bili_unit.reload_settings()
