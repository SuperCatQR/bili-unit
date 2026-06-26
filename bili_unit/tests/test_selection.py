from __future__ import annotations

import pytest

from bili_unit._selection import SelectionError, resolve_subset

ALL = ["a", "b", "c", "d"]


def test_resolve_subset_no_flags_means_all() -> None:
    assert (
        resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=None,
            exclude=None,
        )
        is None
    )


def test_resolve_subset_include_preserves_order() -> None:
    assert resolve_subset(
        flag_label="endpoint",
        all_names=ALL,
        include=["c", "a"],
        exclude=None,
    ) == ["c", "a"]


def test_resolve_subset_exclude_preserves_registered_order() -> None:
    assert resolve_subset(
        flag_label="endpoint",
        all_names=ALL,
        include=None,
        exclude=["b", "d"],
    ) == ["a", "c"]


def test_resolve_subset_rejects_unknown_and_empty() -> None:
    with pytest.raises(SelectionError, match="unknown endpoint"):
        resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=["missing"],
            exclude=None,
        )

    with pytest.raises(SelectionError, match="nothing to run"):
        resolve_subset(
            flag_label="endpoint",
            all_names=ALL,
            include=None,
            exclude=ALL,
        )
