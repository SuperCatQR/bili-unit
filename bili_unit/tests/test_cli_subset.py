# Tests for the CLI exclude/include subset translator.
#
# The unified CLI (``bili-unit``) lets users say "run everything
# except X" via ``-x``. Translation from (include, exclude) → the include-list
# downstream layers expect happens in ``_resolve_subset``. These tests pin
# that contract so a typo in either flag fails fast.

import pytest

from bili_unit.__main__ import _resolve_subset

ALL = ["a", "b", "c", "d"]


def test_no_flags_means_run_everything():
    """Both None → keep downstream "all registered" expansion (return None)."""
    assert (
        _resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=None,
            exclude=None,
        )
        is None
    )


def test_include_passes_through():
    """``-e`` with known names returns the same list."""
    assert _resolve_subset(
        flag_label="endpoint",
        all_names=ALL,
        include=["a", "c"],
        exclude=None,
    ) == ["a", "c"]


def test_include_unknown_name_raises():
    """Typo in ``-e`` should error rather than silently drop."""
    with pytest.raises(SystemExit):
        _resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=["a", "typo"],
            exclude=None,
        )


def test_exclude_drops_named():
    """``-x b`` removes only ``b``; order of remaining names is preserved."""
    assert _resolve_subset(
        flag_label="endpoint",
        all_names=ALL,
        include=None,
        exclude=["b"],
    ) == ["a", "c", "d"]


def test_exclude_multiple_drops_all_named():
    assert _resolve_subset(
        flag_label="endpoint",
        all_names=ALL,
        include=None,
        exclude=["b", "d"],
    ) == ["a", "c"]


def test_exclude_unknown_name_raises():
    """Typo in ``-x`` should error rather than silently no-op."""
    with pytest.raises(SystemExit):
        _resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=None,
            exclude=["typo"],
        )


def test_exclude_everything_raises():
    """``-x`` must not produce an empty run."""
    with pytest.raises(SystemExit):
        _resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=None,
            exclude=ALL,
        )


def test_argparse_layer_rejects_both_flags():
    """``-e`` and ``-x`` are mutually exclusive on the fetch parser."""
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "-e", "user_info", "-x", "videos"])


def test_default_subset_is_none_for_fetch():
    """Without flags, fetch parsers leave endpoint include/exclude unset."""
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1"])
    assert args.endpoints is None
    assert args.exclude_endpoints is None


def test_fetch_accepts_generic_include_exclude_aliases():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1", "--include", "user_info"])
    assert args.endpoints == ["user_info"]
    args = parser.parse_args(["fetch", "1", "--exclude", "videos"])
    assert args.exclude_endpoints == ["videos"]


# --- Tests for --profile ----------------------------------------------------


def test_profile_default_is_all():
    """No --profile flag → defaults to "all"."""
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1"])
    assert args.profile == "all"


def test_profile_short_flag():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fetch", "1", "-p", "minimal"])
    assert args.profile == "minimal"


def test_profile_unknown_rejected():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "--profile", "everything"])


def test_profile_mutually_exclusive_with_endpoints():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "-p", "minimal", "-e", "user_info"])


def test_profile_mutually_exclusive_with_exclude():
    from bili_unit.__main__ import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "1", "-p", "minimal", "-x", "videos"])
