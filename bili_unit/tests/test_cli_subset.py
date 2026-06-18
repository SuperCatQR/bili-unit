# Tests for the CLI exclude/include subset translator.
#
# The unified CLI (``python -m bili_unit``) lets users say "run everything
# except X" via ``-x``. Translation from (include, exclude) → the include-list
# downstream layers expect happens in ``_resolve_subset``. These tests pin
# that contract so a typo in either flag fails fast.

import pytest

from bili_unit.__main__ import _resolve_subset

ALL = ["a", "b", "c", "d"]


def test_no_flags_means_run_everything():
    """Both None → keep downstream "all registered" expansion (return None)."""
    assert _resolve_subset(
        flag_label="endpoint", all_names=ALL, include=None, exclude=None,
    ) is None


def test_include_passes_through():
    """``-e`` with known names returns the same list."""
    assert _resolve_subset(
        flag_label="endpoint", all_names=ALL, include=["a", "c"], exclude=None,
    ) == ["a", "c"]


def test_include_unknown_name_raises():
    """Typo in ``-e`` should error rather than silently drop."""
    with pytest.raises(SystemExit):
        _resolve_subset(
            flag_label="endpoint", all_names=ALL,
            include=["a", "typo"], exclude=None,
        )


def test_exclude_drops_named():
    """``-x b`` removes only ``b``; order of remaining names is preserved."""
    assert _resolve_subset(
        flag_label="endpoint", all_names=ALL,
        include=None, exclude=["b"],
    ) == ["a", "c", "d"]


def test_exclude_multiple_drops_all_named():
    assert _resolve_subset(
        flag_label="endpoint", all_names=ALL,
        include=None, exclude=["b", "d"],
    ) == ["a", "c"]


def test_exclude_unknown_name_raises():
    """Typo in ``-x`` should error rather than silently no-op."""
    with pytest.raises(SystemExit):
        _resolve_subset(
            flag_label="endpoint", all_names=ALL,
            include=None, exclude=["typo"],
        )


def test_exclude_everything_raises():
    """``-x`` must not produce an empty run."""
    with pytest.raises(SystemExit):
        _resolve_subset(
            flag_label="endpoint", all_names=ALL,
            include=None, exclude=ALL,
        )


def test_argparse_layer_rejects_both_flags():
    """``-e`` and ``-x`` are mutually exclusive on fetch-like parsers."""
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "-e", "user_info", "-x", "videos"])
    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "1", "-e", "user_info", "-x", "videos"])
    with pytest.raises(SystemExit):
        parser.parse_args(["parse", "1", "-e", "video_work", "-x", "opus_post"])


def test_default_subset_is_none_for_fetch():
    """Without flags, fetch-like parsers leave endpoint include/exclude unset.

    The ``process`` subcommand no longer takes -e/-x style item-type flags after
    the 2026-06-14 transform deletion (only one pipeline remains: audio).
    """
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1"])
    assert args.endpoints is None
    assert args.exclude_endpoints is None
    args = parser.parse_args(["sync", "1"])
    assert args.endpoints is None
    assert args.exclude_endpoints is None


def test_fetch_accepts_generic_include_exclude_aliases():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1", "--include", "user_info"])
    assert args.endpoints == ["user_info"]
    args = parser.parse_args(["fetch", "1", "--exclude", "videos"])
    assert args.exclude_endpoints == ["videos"]


def test_parse_accepts_model_include_exclude():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["parse", "1", "-e", "video_work", "opus_post"])
    assert args.models == ["video_work", "opus_post"]
    assert args.exclude_models is None

    args = parser.parse_args(["parse", "1", "--exclude", "video_subtitle"])
    assert args.models is None
    assert args.exclude_models == ["video_subtitle"]


def test_resolve_parse_models_validates_known_models():
    from bili_unit.__main__ import _build_parser, _resolve_parse_models

    parser = _build_parser()
    args = parser.parse_args(["parse", "1", "-e", "video_work", "opus_post"])
    assert _resolve_parse_models(args) == ["video_work", "opus_post"]

    args = parser.parse_args(["parse", "1", "-x", "video_subtitle"])
    assert _resolve_parse_models(args) == [
        "user_profile",
        "video_work",
        "article_post",
        "opus_post",
        "dynamic_event",
    ]

    args = parser.parse_args(["parse", "1", "-e", "typo"])
    with pytest.raises(SystemExit):
        _resolve_parse_models(args)


# --- Tests for --profile (issue #2) ----------------------------------------

def test_profile_default_is_all():
    """No --profile flag → defaults to "all" (backward compat)."""
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1"])
    assert args.profile == "all"
    args = parser.parse_args(["sync", "1"])
    assert args.profile == "all"


def test_sync_argparse_defaults_and_modes():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["sync", "1"])
    assert args.fetch_mode == "incremental"
    assert args.parse_mode == "incremental"
    assert args.download_images is False
    assert args.models is None
    assert args.exclude_models is None

    args = parser.parse_args([
        "sync", "1",
        "--fetch-mode", "full",
        "--parse-mode", "incremental",
        "--download-images",
    ])
    assert args.fetch_mode == "full"
    assert args.parse_mode == "incremental"
    assert args.download_images is True

    args = parser.parse_args(["sync", "1", "--models", "video_work"])
    assert args.models == ["video_work"]
    assert args.exclude_models is None

    args = parser.parse_args(["sync", "1", "--exclude-models", "video_subtitle"])
    assert args.models is None
    assert args.exclude_models == ["video_subtitle"]

    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "1", "--models", "video_work", "--exclude-models", "opus_post"])


def test_profile_parsing_chosen():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1", "--profile", "parsing"])
    assert args.profile == "parsing"


def test_profile_short_flag():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1", "-p", "minimal"])
    assert args.profile == "minimal"


def test_resolve_fetch_endpoints_uses_parsing_profile_for_sync():
    from bili_unit.__main__ import _build_parser, _resolve_fetch_endpoints
    from bili_unit.fetching._endpoint_catalog import PROFILES

    parser = _build_parser()
    args = parser.parse_args(["sync", "1", "--profile", "parsing"])
    endpoints = _resolve_fetch_endpoints(args)

    assert endpoints is not None
    assert set(endpoints) == set(PROFILES["parsing"])
    assert "video_subtitle" in endpoints


def test_profile_unknown_rejected():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "--profile", "everything"])


def test_profile_mutually_exclusive_with_endpoints():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "-p", "parsing", "-e", "user_info"])


def test_profile_mutually_exclusive_with_exclude():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "-p", "parsing", "-x", "videos"])
