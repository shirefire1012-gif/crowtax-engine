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


# --- States ------------------------------------------------------------------


# Order matches the v1.2 plan: top-10 income-tax + 8 no-income-tax stubs.
# WA is added in a later commit (excise branch); included here just to
# verify the smoke test parametrizes cleanly across whatever ships.
V12_STATES_INCOME_TAX = ["ca", "ny", "nj", "il", "ma", "pa", "ga", "nc"]
V12_STATES_NO_TAX = ["tx", "fl", "ak", "nv", "nh", "sd", "tn", "wy"]
V12_STATES_EXCISE = ["wa"]
V12_STATES_ALL = V12_STATES_INCOME_TAX + V12_STATES_NO_TAX + V12_STATES_EXCISE


@pytest.mark.parametrize("state_code", V12_STATES_ALL)
def test_load_each_v12_state_smoke(state_code):
    """Every shipped state YAML loads cleanly and has the required fields."""
    rs = load_ruleset(2026, state=state_code)
    eff = rs.effective
    assert eff["tax_year"] == 2026
    assert eff["capital_gains_treatment"] in (
        "ordinary",
        "preferential_rate",
        "exclusion_pct",
        "subtraction_pct",
        "excise",
        "none",
    )
    assert isinstance(eff["no_income_tax"], bool)
    assert isinstance(eff["is_excise_tax"], bool)
    assert isinstance(eff["loss_carryforward"], bool)


@pytest.mark.parametrize("state_code", V12_STATES_NO_TAX)
def test_no_tax_states_marked_correctly(state_code):
    rs = load_ruleset(2026, state=state_code)
    assert rs.effective["no_income_tax"] is True
    assert rs.effective["capital_gains_treatment"] == "none"


def test_ca_static_date_conformity():
    rs = load_ruleset(2026, state="ca")
    eff = rs.effective
    assert eff["conformity_type"] == "static_date"
    assert eff["conformity_date"] == "2015-01-01"
    assert eff["has_ltcg_preference"] is False
    assert eff["capital_gains_treatment"] == "ordinary"
    # Top bracket 13.3%
    top_rates = [b["rate"] for b in eff["brackets"]]
    assert max(top_rates) == pytest.approx(0.133)


def test_il_flat_rate():
    rs = load_ruleset(2026, state="il")
    assert rs.effective["flat_rate"] == 0.0495
    assert "brackets" not in rs.state_layer  # IL is flat-only


def test_pa_no_loss_carryforward():
    rs = load_ruleset(2026, state="pa")
    assert rs.effective["loss_carryforward"] is False
    assert rs.effective["flat_rate"] == 0.0307


def test_nj_no_schedule_d():
    rs = load_ruleset(2026, state="nj")
    assert rs.effective["has_schedule_d"] is False
    assert rs.effective["loss_carryforward"] is False


def test_ma_stcg_ltcg_split():
    rs = load_ruleset(2026, state="ma")
    eff = rs.effective
    assert eff["capital_gains_treatment"] == "preferential_rate"
    assert eff["capital_gains_rate"] == pytest.approx(0.05)
    assert eff["short_term_capital_gains_rate"] == pytest.approx(0.085)
    assert eff["has_ltcg_preference"] is True


def test_ga_static_date_2023():
    rs = load_ruleset(2026, state="ga")
    assert rs.effective["conformity_date"] == "2023-01-01"
    assert rs.effective["flat_rate"] == 0.0549


def test_nc_static_date_2024():
    rs = load_ruleset(2026, state="nc")
    assert rs.effective["conformity_date"] == "2024-05-01"
    assert rs.effective["flat_rate"] == 0.045


def test_wy_no_tax():
    rs = load_ruleset(2026, state="wy")
    assert rs.effective["no_income_tax"] is True


def test_wa_excise():
    """WA: no broad income tax, but a 7% capital-gains excise over $262k."""
    rs = load_ruleset(2026, state="wa")
    eff = rs.effective
    assert eff["is_excise_tax"] is True
    assert eff["no_income_tax"] is True
    assert eff["capital_gains_treatment"] == "excise"
    excise = eff["capital_gains_excise"]
    assert excise["rate"] == pytest.approx(0.07)
    assert excise["threshold"] == 262_000
    assert excise["applies_to"] == "long_term_only"
    assert excise["domicile_based"] is True
    assert excise["charitable_deduction_cap"] == 100_000
