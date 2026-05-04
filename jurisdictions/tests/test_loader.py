"""
Unit tests for ``jurisdictions.loader``.

This file is split into two layers:

1. **Self-contained unit tests** (this commit) — exercise the merge,
   validation, and error-path helpers without needing any YAML on disk.
2. **YAML-dependent integration tests** (added in subsequent commits as
   federal/state/city YAMLs land) — see the ``test_loader_yaml.py``
   companion file once those files exist.
"""

from __future__ import annotations

import pytest

from jurisdictions.loader import (
    _deep_merge,
    _validate_required_fields,
    load_ruleset,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    load_ruleset.cache_clear()
    yield
    load_ruleset.cache_clear()


def test_missing_file_no_fallback():
    """``allow_fallback=False`` should raise immediately, no walking."""
    with pytest.raises(FileNotFoundError):
        load_ruleset(2030, state="zz", allow_fallback=False)


def test_unknown_state_walks_back_then_raises():
    """No fallback file in 5-year window → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_ruleset(2026, state="zz")


def test_missing_required_field_raises():
    bad_effective = {
        "tax_year": 2026,
        # missing has_ltcg_preference, capital_gains_treatment, etc.
    }
    with pytest.raises(ValueError) as exc_info:
        _validate_required_fields(bad_effective, 2026, None, None)
    msg = str(exc_info.value)
    assert "has_ltcg_preference" in msg
    assert "capital_gains_treatment" in msg
    assert "loss_carryforward" in msg


def test_validate_passes_when_all_required_present():
    good = {
        "tax_year": 2026,
        "has_ltcg_preference": True,
        "capital_gains_treatment": "preferential_rate",
        "loss_carryforward": True,
        "no_income_tax": False,
        "is_excise_tax": False,
    }
    # Should not raise
    _validate_required_fields(good, 2026, None, None)


def test_deep_merge_list_replacement():
    """Confirm a later layer's list fully replaces, never extends."""
    fed = {"brackets": [{"rate": 0.10}, {"rate": 0.12}]}
    state = {"brackets": [{"rate": 0.05}]}
    merged = _deep_merge(fed, state)
    assert merged["brackets"] == [{"rate": 0.05}]


def test_deep_merge_nested_dict_recurse():
    fed = {"thresholds": {"single": 200_000, "mfj": 250_000}}
    state = {"thresholds": {"single": 100_000}}
    merged = _deep_merge(fed, state)
    assert merged["thresholds"] == {"single": 100_000, "mfj": 250_000}


def test_deep_merge_three_layers_left_to_right():
    """Federal → state → city: city wins on shared keys, but its own
    additions are preserved alongside non-overlapping federal/state."""
    fed = {"a": 1, "b": 2, "c": 3}
    state = {"b": 20, "d": 4}
    city = {"c": 300}
    merged = _deep_merge(fed, state, city)
    assert merged == {"a": 1, "b": 20, "c": 300, "d": 4}


def test_deep_merge_skips_falsy_layers():
    fed = {"a": 1}
    merged = _deep_merge(fed, None, {})
    assert merged == {"a": 1}


def test_deep_merge_scalar_overrides_dict():
    """If a later layer puts a scalar where the earlier had a dict, the
    scalar wins (no merge attempt)."""
    fed = {"k": {"nested": True}}
    state = {"k": "scalar"}
    merged = _deep_merge(fed, state)
    assert merged["k"] == "scalar"
