# test_parsing_protocol.py — structural conformance check.
#
# Verifies that :class:`bili_unit.parsing.query.ParsingQuery` exposes every
# method declared on :class:`bili_unit.parsing.protocols.ParsingReadView`.
# This is a lightweight name-set assertion, not a full type-signature check.

import pytest

from bili_unit.parsing.protocols import ParsingReadView
from bili_unit.parsing.query import ParsingQuery

# ParsingReadView / ParsingQuery are part of the legacy Python read facade
# slated for deletion in Phase 4. Keep the import paths but skip the
# assertion; consumers query SQLite directly now.
pytestmark = pytest.mark.skip(
    reason="moved to Phase 6 rewrite — read API replaced by direct SQL in Phase 4",
)


def test_parsing_query_implements_parsing_read_view():
    """ParsingQuery has every method declared on ParsingReadView."""
    expected = {
        m for m in dir(ParsingReadView)
        if not m.startswith("_")
    }
    actual = {m for m in dir(ParsingQuery) if not m.startswith("_")}
    missing = expected - actual
    assert not missing, f"ParsingQuery missing protocol methods: {missing}"
