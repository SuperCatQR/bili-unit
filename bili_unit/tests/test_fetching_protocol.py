# test_fetching_protocol.py — structural conformance check.
#
# Verifies that :class:`bili_unit.fetching.query.Query` exposes every method
# declared on :class:`bili_unit.fetching.protocols.FetchingReadView`.
# This is a lightweight name-set assertion, not a full type-signature check.

from bili_unit.fetching.protocols import FetchingReadView
from bili_unit.fetching.query import Query


def test_query_implements_fetching_read_view():
    """Query has every method declared on FetchingReadView."""
    expected = {
        m for m in dir(FetchingReadView)
        if not m.startswith("_")
    }
    actual = {m for m in dir(Query) if not m.startswith("_")}
    missing = expected - actual
    assert not missing, f"Query missing protocol methods: {missing}"
