# test_parsing_protocol.py — structural conformance check.
#
# Verifies that :class:`bili_unit.parsing.query.ParsingQuery` exposes every
# method declared on :class:`bili_unit.parsing.protocols.ParsingReadView`.
# This is a lightweight name-set assertion, not a full type-signature check.

from bili_unit.parsing.protocols import ParsingReadView
from bili_unit.parsing.query import ParsingQuery


def test_parsing_query_implements_parsing_read_view():
    """ParsingQuery has every method declared on ParsingReadView."""
    expected = {
        m for m in dir(ParsingReadView)
        if not m.startswith("_")
    }
    actual = {m for m in dir(ParsingQuery) if not m.startswith("_")}
    missing = expected - actual
    assert not missing, f"ParsingQuery missing protocol methods: {missing}"
