"""Tests for the engine-side transfer-pair skip-list interface.

The engine is a math library — it doesn't query a database. The
caller (server, dashboard, third-party consumers) loads
transfer-pair data, builds an ExclusionSet, and passes it through.
"""

from __future__ import annotations

from crowtax_engine.transfer_pairs import ExclusionSet, is_excluded_event


def test_empty_set_excludes_nothing() -> None:
    s = ExclusionSet(out_event_ids=frozenset(), in_event_ids=frozenset())
    assert is_excluded_event("evt-1", s) is False


def test_set_excludes_listed_events() -> None:
    s = ExclusionSet(
        out_event_ids=frozenset({"out-1"}),
        in_event_ids=frozenset({"in-1"}),
    )
    assert is_excluded_event("out-1", s) is True
    assert is_excluded_event("in-1", s) is True
    assert is_excluded_event("other", s) is False


def test_exclusion_set_is_hashable_and_immutable() -> None:
    """Frozen dataclass + frozensets means callers can put it in a set
    or use it as a memoization key."""
    a = ExclusionSet(out_event_ids=frozenset({"a"}), in_event_ids=frozenset({"b"}))
    b = ExclusionSet(out_event_ids=frozenset({"a"}), in_event_ids=frozenset({"b"}))
    assert a == b
    assert hash(a) == hash(b)
