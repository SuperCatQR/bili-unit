"""Shared include/exclude selection helpers for command surfaces."""

from __future__ import annotations


class SelectionError(ValueError):
    """Raised when a requested include/exclude subset is invalid."""


def resolve_subset(
    *,
    flag_label: str,
    all_names: list[str],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[str] | None:
    """Translate include/exclude flags into the include-list passed downstream.

    ``None`` means "run everything" so the receiving layer can keep its own
    registered-order expansion. This function is intentionally UI-neutral:
    CLI, future TUI actions, and tests should share the same semantics.
    """
    known = set(all_names)

    if include is not None:
        unknown = [name for name in include if name not in known]
        if unknown:
            raise SelectionError(
                f"unknown {flag_label}(s): {', '.join(unknown)}. "
                f"Known: {', '.join(all_names)}",
            )
        return list(include)

    if exclude is not None:
        unknown = [name for name in exclude if name not in known]
        if unknown:
            raise SelectionError(
                f"unknown {flag_label}(s) in --exclude: {', '.join(unknown)}. "
                f"Known: {', '.join(all_names)}",
            )
        excluded = set(exclude)
        kept = [name for name in all_names if name not in excluded]
        if not kept:
            raise SelectionError(
                f"--exclude removed every {flag_label}; nothing to run.",
            )
        return kept

    return None


__all__ = ["SelectionError", "resolve_subset"]
