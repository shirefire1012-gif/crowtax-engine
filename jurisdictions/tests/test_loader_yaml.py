"""
Integration tests for ``jurisdictions.loader`` against on-disk YAML.

These run against the actual YAML files shipped in the package, not
synthetic fixtures — the goal is to catch typos, mis-spelled rate
keys, and accidental schema drift between the loader and the data.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from unittest import mock

import pytest

from jurisdictions.loader import EffectiveRuleset, load_ruleset


@pytest.fixture(autouse=True)
def _clear_cache():
    load_ruleset.cache_clear()
    yield
    load_ruleset.cache_clear()


# --- Federal ----------------------------------------------------------------


def test_load_federal_only():
    rs = load_ruleset(2026)
    assert isinstance(rs, EffectiveRuleset)
    assert rs.tax_year == 2026
    assert rs.state is None
    assert rs.city is None
    assert rs.fallback_years == {}

    eff = rs.effective
    assert eff["niit_applies"] is True
    assert eff["niit_rate"] == 0.038
    assert eff["has_ltcg_preference"] is True
    assert eff["capital_gains_treatment"] == "preferential_rate"
    assert eff["no_income_tax"] is False
    assert eff["is_excise_tax"] is False

    # Verify NIIT thresholds present and correct.
    nt = eff["niit_thresholds"]
    assert nt["single"] == 200_000
    assert nt["mfj"] == 250_000

    # Verify ordinary + LTCG bracket schedules present and well-shaped.
    assert any(
        b["filing_status"] == "single" and b["threshold"] == 11925
        and b["rate"] == 0.12
        for b in eff["ordinary_brackets"]
    )
    assert any(
        b["filing_status"] == "single" and b["threshold"] == 48350
        and b["rate"] == 0.15
        for b in eff["ltcg_brackets"]
    )


def test_federal_yaml_has_all_filing_statuses_for_ltcg():
    rs = load_ruleset(2026)
    statuses = {b["filing_status"] for b in rs.effective["ltcg_brackets"]}
    assert statuses == {"single", "mfj", "mfs", "hoh"}


def test_federal_yaml_has_all_filing_statuses_for_ordinary():
    rs = load_ruleset(2026)
    statuses = {b["filing_status"] for b in rs.effective["ordinary_brackets"]}
    assert statuses == {"single", "mfj", "mfs", "hoh"}


def test_federal_standard_deduction_block():
    rs = load_ruleset(2026)
    sd = rs.effective["standard_deduction"]
    assert sd["single"] == 15700
    assert sd["mfj"] == 31400


# --- Fallback / cache --------------------------------------------------------


def test_fallback_year_emits_warning():
    """Request year 2030 — federal/2030.yaml does not exist; should fall
    back to 2026.yaml and emit a UserWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rs = load_ruleset(2030)
    assert rs.fallback_years.get("federal") == 2026
    assert any(
        issubclass(w.category, UserWarning)
        and "fallback years" in str(w.message).lower()
        for w in caught
    )


def test_lru_cache_hit_avoids_disk_read():
    load_ruleset.cache_clear()
    real_read = Path.read_text
    call_count = {"n": 0}

    def counting_read(self, *a, **kw):
        call_count["n"] += 1
        return real_read(self, *a, **kw)

    with mock.patch.object(Path, "read_text", counting_read):
        load_ruleset(2026)
        first = call_count["n"]
        load_ruleset(2026)
        second = call_count["n"]
    assert second == first, "second call should hit lru_cache, not re-read"


def test_source_files_recorded_for_federal():
    rs = load_ruleset(2026)
    assert any("federal/2026.yaml" in p for p in rs.source_files)
