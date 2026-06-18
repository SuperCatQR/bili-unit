from __future__ import annotations

import re
from pathlib import Path

import pytest

from bili_unit import __main__ as cli
from bili_unit._db.connection import (
    SUPPORTED_MAIN_SCHEMA_VERSION,
    SUPPORTED_RAW_SCHEMA_VERSION,
)
from bili_unit._env import BiliSettings
from bili_unit.fetching._endpoint_catalog import ENDPOINTS, resolve_profile

ROOT = Path(__file__).resolve().parents[2]
CURRENT_DOCS = [
    ROOT / ".env.example",
    ROOT / "README.md",
    ROOT / "CONTEXT.md",
    ROOT / "docs" / "schema.md",
    ROOT / "docs" / "observability.md",
    ROOT / "docs" / "upstream.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "endpoint-contract.md",
]
EXPECTED_DOC_FILES = {
    "README.md",
    "schema.md",
    "observability.md",
    "upstream.md",
    "architecture.md",
    "endpoint-contract.md",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_docs_tree_has_only_current_contract_files() -> None:
    docs_root = ROOT / "docs"
    actual = {
        path.relative_to(docs_root).as_posix()
        for path in docs_root.rglob("*.md")
    }

    assert actual == EXPECTED_DOC_FILES
    assert not (docs_root / "history").exists()
    assert not (docs_root / "adr").exists()
    assert not (docs_root / "feature").exists()
    assert not (docs_root / "structure").exists()
    assert not (docs_root / "feature" / "processing.md").exists()


def test_ddl_header_versions_match_supported_versions() -> None:
    main = _read(ROOT / "bili_unit" / "_db" / "ddl" / "main_v4.sql")
    raw = _read(ROOT / "bili_unit" / "_db" / "ddl" / "raw_v2.sql")

    assert f"schema, version {SUPPORTED_MAIN_SCHEMA_VERSION}" in main
    assert f"schema, version {SUPPORTED_RAW_SCHEMA_VERSION}" in raw
    assert "default: output/bili/{uid}.db" in main
    assert "default: output/bili/{uid}.raw.db" in raw


def test_schema_doc_versions_match_supported_versions() -> None:
    schema = _read(ROOT / "docs" / "schema.md")

    assert f"schema_version = {SUPPORTED_MAIN_SCHEMA_VERSION}" in schema
    assert f"schema_version = {SUPPORTED_RAW_SCHEMA_VERSION}" in schema
    assert f"schema_version` 当前为 `'{SUPPORTED_MAIN_SCHEMA_VERSION}'" in schema
    assert f"schema_version` 当前为 `'{SUPPORTED_RAW_SCHEMA_VERSION}'" in schema


def test_endpoint_counts_in_docs_match_registry() -> None:
    readme = _read(ROOT / "README.md")
    context = _read(ROOT / "CONTEXT.md")
    contract = _read(ROOT / "docs" / "endpoint-contract.md")
    total = len(ENDPOINTS)
    uid_level = sum(1 for endpoint in ENDPOINTS if endpoint.source_endpoint is None)
    item_level = total - uid_level
    documented = 29
    undocumented = total - documented

    assert f"{total} 个读取端点" in readme
    assert f"{total} 个端点的 raw_payload schema" in readme
    assert f"{total} 个 B 站读取端点" in context
    assert f"共 {total} 个（{uid_level} uid-level + {item_level} item-level）" in context
    assert f"`all`={total}" in context
    assert f"`parsing`={len(resolve_profile('parsing'))}" in context
    assert f"`minimal`={len(resolve_profile('minimal'))}" in context
    assert f"{documented} / {total} 个注册端点" in contract
    assert f"剩余 {undocumented} 个端点" in contract
    assert f"下列 {undocumented} 个端点" in contract


def test_env_example_keys_are_real_settings_fields() -> None:
    env_text = _read(ROOT / ".env.example")
    documented_keys = set(re.findall(r"^#?\s*(BILI_[A-Z0-9_]+)=", env_text, re.MULTILINE))
    settings_keys = {
        field_name.upper()
        for field_name in BiliSettings.model_fields
        if field_name.startswith("bili_")
    }

    assert documented_keys - settings_keys == set()
    assert {
        "BILI_DB_DIR",
        "BILI_PROCESSING_TEMP_DIR",
        "BILI_PROCESSING_ASR_CACHE_DIR",
    } <= documented_keys


def test_env_example_defaults_match_settings() -> None:
    env_text = _read(ROOT / ".env.example")
    settings = BiliSettings()

    expected = {
        "BILI_DB_DIR": settings.bili_db_dir,
        "BILI_PROCESSING_TEMP_DIR": settings.bili_processing_temp_dir,
        "BILI_PROCESSING_ASR_CACHE_DIR": settings.bili_processing_asr_cache_dir,
        "BILI_PROCESSING_ASR_BACKEND": settings.bili_processing_asr_backend,
    }
    for key, value in expected.items():
        assert re.search(rf"^#?\s*{key}={re.escape(str(value))}\b", env_text, re.MULTILINE)


def test_user_visible_cli_examples_parse() -> None:
    parser = cli._build_parser()
    examples = [
        ["sync", "123456"],
        ["asr", "123456"],
        ["asr", "123456", "-b", "mock"],
        ["tui"],
        ["delete-uid", "123456", "-y"],
        ["init-mimo", "--test"],
    ]

    for argv in examples:
        parser.parse_args(argv)


@pytest.mark.parametrize("path", CURRENT_DOCS)
def test_current_docs_do_not_expose_removed_cli_or_storage_contracts(path: Path) -> None:
    text = _read(path)

    forbidden = [
        "process -b",
        "python -m bili_unit process",
        "schema, version 3",
        "data/bili/{uid}",
        "BILI_FETCHING_DATA_DIR",
        "BILI_FETCHING_ERROR_DIR",
        "BILI_MANIFEST_DIR",
        "BILI_PROCESSING_DATA_DIR",
        "BILI_PROCESSING_ERROR_DIR",
        "output/bili/processing/temp",
        "output/bili/processing/asr_cache",
        "parsing / processing",
        "processing 通过 `FetchingStore` 读取 raw DB",
        "workdir/images/",
        "video_cover",
        "opus_image",
        "article_image",
    ]
    for phrase in forbidden:
        assert phrase not in text, f"{path} contains outdated phrase: {phrase}"


def test_user_visible_docs_expose_asr_not_processing_command() -> None:
    readme = _read(ROOT / "README.md")
    context = _read(ROOT / "CONTEXT.md")
    upstream = _read(ROOT / "docs" / "upstream.md")

    assert "uv run bili-unit asr <uid>" in readme
    assert "CLI does not\nexpose it" in context
    assert "→ asr" in upstream
