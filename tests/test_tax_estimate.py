"""
Golden-file tests for ``crowtax_engine.tax_estimate``.

Each test loads a real ruleset (no YAML mocking) and asserts computed
figures against hand-calculated expected values. Tolerance is $1 — the
goal is to catch logic regressions, not to match a CPA's spreadsheet to
the cent.
"""

from __future__ import annotations

import warnings

import pytest

from crowtax_engine.tax_estimate import (
    JurisdictionTaxResult,
    compute_jurisdiction_tax,
)
from jurisdictions.loader import load_ruleset


@pytest.fixture(autouse=True)
def _suppress_fallback_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        yield


# ----- Federal-only ----------------------------------------------------------


def test_federal_only_single_50k_ltcg_no_ordinary():
    """
    Federal only, single, $50k LTCG, $0 ordinary income.

    LTCG: at $0 ordinary, $50k LTCG sits across the 0% (0-48,350) and
    15% (48,351-533,400) brackets. Tax = $0 + $1,650 * 15% = $247.50.
    NIIT: MAGI $50k < $200k threshold → $0.
    """
    rs = load_ruleset(2026)
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=0.0,
        ruleset=rs,
        filing_status="single",
    )
    assert isinstance(result, JurisdictionTaxResult)
    assert result.federal_ordinary_tax_usd == pytest.approx(0.0, abs=1.0)
    # 50000 - 48350 = 1650 at 15% = 247.50
    assert result.federal_ltcg_tax_usd == pytest.approx(247.50, abs=1.0)
    assert result.niit_usd == pytest.approx(0.0, abs=0.01)
    assert result.state_tax_usd == 0.0
    assert result.city_tax_usd == 0.0
    assert result.total_estimated_tax_usd == pytest.approx(247.50, abs=1.0)


def test_federal_only_high_earner_niit_kicks_in():
    """
    Federal only, single, $50k LTCG, $300k ordinary income.

    LTCG: $50k entirely at 15% (since base $300k > $48,350) = $7,500.
    NIIT: MAGI = $350k. Excess over $200k = $150k.
        net_investment_income = $50k. min($50k, $150k) * 3.8% = $1,900.
    Federal ordinary on the $0 ST layer = $0.
    """
    rs = load_ruleset(2026)
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=300_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.federal_ltcg_tax_usd == pytest.approx(7_500.00, abs=1.0)
    assert result.niit_usd == pytest.approx(1_900.00, abs=1.0)


def test_federal_short_term_marginal_on_top():
    """
    Federal only, single, $20k STCG, $0 LTCG, $80k ordinary income.

    Base ordinary $80k sits in the 22% bracket (48,475-103,350).
    STCG $20k stacks: $20k in the 22% bracket, partially crossing
    into 24% if it exceeds $103,350. $80k+$20k = $100k, still in 22%.
    Marginal STCG tax = $20,000 * 22% = $4,400.
    """
    rs = load_ruleset(2026)
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=20_000.0,
        lt_cap_gain_usd=0.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.federal_ordinary_tax_usd == pytest.approx(4_400.00, abs=1.0)
    assert result.federal_ltcg_tax_usd == pytest.approx(0.0, abs=0.01)


# ----- No-tax states ---------------------------------------------------------


@pytest.mark.parametrize("state", ["tx", "fl", "ak", "nv", "nh", "sd", "tn", "wy"])
def test_no_tax_states_zero_state_tax(state):
    rs = load_ruleset(2026, state=state)
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=10_000.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == 0.0
    assert result.city_tax_usd == 0.0
    # Federal still applies.
    assert result.federal_ltcg_tax_usd > 0


# ----- WA excise -------------------------------------------------------------


def test_wa_excise_below_threshold_no_state_tax():
    rs = load_ruleset(2026, state="wa")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=200_000.0,  # below $262k threshold
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == 0.0
    # Federal LTCG still applies.
    assert result.federal_ltcg_tax_usd > 0


def test_wa_excise_above_threshold():
    """
    WA, single, $300k LTCG.
    Excise: ($300k - $262k) * 7% = $38k * 0.07 = $2,660.
    Federal LTCG at 15% (high earner band) = $300k * 15% ~= $45k.
    """
    rs = load_ruleset(2026, state="wa")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=300_000.0,
        ordinary_income_usd=0.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == pytest.approx(2_660.00, abs=1.0)
    # Federal LTCG: with $0 ordinary, the $300k spans 0% (0-48350),
    # 15% (48,351-533,400). Tax = (300,000 - 48,350) * 15% = $37,747.50
    assert result.federal_ltcg_tax_usd == pytest.approx(37_747.50, abs=2.0)
    # NIIT applies: MAGI $300k > $200k. Excess = $100k. NII = $300k.
    # min(300k, 100k) * 3.8% = $3,800.
    assert result.niit_usd == pytest.approx(3_800.00, abs=1.0)


# ----- Bracket states (CA, NY) -----------------------------------------------


def test_ca_resident_50k_ltcg_80k_ordinary():
    """
    CA: no LTCG preference; gains taxed as ordinary at CA brackets.
    $80k ordinary + $50k LTCG = $130k CA taxable. Marginal CA rate at
    $130k single is 9.3%. State tax on the $50k cap-gain layer:
    spans $70,606-$130,000 at 9.3%; for the slice from $80k-$130k.
    Hand calc: total CA tax(130k) - total CA tax(80k).
        At 80k: 0.01*10756 + 0.02*(25500-10756) + 0.04*(40246-25500)
              + 0.06*(55866-40246) + 0.08*(70606-55866)
              + 0.093*(80000-70606)
              = 107.56 + 294.88 + 589.84 + 937.20 + 1179.20 + 873.64
              = 3982.32
        At 130k: 3982.32 + 0.093*(130000-80000) = 3982.32 + 4650 = 8632.32
        Marginal on the $50k cap-gain layer = $4,650.
    """
    rs = load_ruleset(2026, state="ca")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == pytest.approx(4_650.00, abs=2.0)
    assert result.state_taxable_cap_gain_usd == pytest.approx(50_000.00, abs=0.01)


def test_ny_resident_no_city():
    """
    NY: rolling, ordinary treatment. $50k LTCG on $80k ordinary = $130k NY.
    Brackets: 0.04 (0-8500), 0.045 (8500-11700), 0.0525 (11700-13900),
    0.0585 (13900-80650), 0.0625 (80650-215400), 0.0685 (215400-1077550).
    Marginal on the $50k slice from $80k-$130k:
        80k-80650: $650 at 5.85%
        80650-130000: $49,350 at 6.25%
        Total = 38.025 + 3084.375 = 3122.40
    """
    rs = load_ruleset(2026, state="ny")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == pytest.approx(3_122.40, abs=2.0)


# ----- City overlay (NYC) ----------------------------------------------------


def test_ny_nyc_resident():
    """
    NY + NYC: same NY state tax + NYC bracket layered atop.
    NYC brackets single: 0.03078 (0-12k), 0.03762 (12k-25k),
    0.03819 (25k-50k), 0.03876 (50k+).
    On $80k base + $50k cap gain = $130k:
        At $80k: total NYC = 0.03078*12000 + 0.03762*(25000-12000)
                          + 0.03819*(50000-25000) + 0.03876*(80000-50000)
                = 369.36 + 489.06 + 954.75 + 1162.80 = 2975.97
        At $130k: 2975.97 + 0.03876*(130000-80000) = 2975.97 + 1938 = 4913.97
        Marginal NYC on cap-gain layer = $1,938.
    """
    rs = load_ruleset(2026, state="ny", city="new_york_city")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.city_tax_usd == pytest.approx(1_938.00, abs=5.0)
    assert result.state_tax_usd == pytest.approx(3_122.40, abs=5.0)


def test_yonkers_surcharge():
    """
    Yonkers resident surcharge = 16.75% of NY state tax.
    Same NY scenario above → state_tax = $3,122.40
    Yonkers city tax = $3,122.40 * 0.1675 = $522.99
    """
    rs = load_ruleset(2026, state="ny", city="yonkers")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    expected_city = result.state_tax_usd * 0.1675
    assert result.city_tax_usd == pytest.approx(expected_city, abs=1.0)


def test_philadelphia_no_cap_gains_tax():
    """Philly: no_cap_gains_tax → city = 0; PA flat 3.07% on state."""
    rs = load_ruleset(2026, state="pa", city="philadelphia")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.city_tax_usd == 0.0
    # PA state: flat 3.07% on $50k = $1,535.
    assert result.state_tax_usd == pytest.approx(1_535.00, abs=1.0)


# ----- Flat-rate states ------------------------------------------------------


def test_il_flat_rate():
    rs = load_ruleset(2026, state="il")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == pytest.approx(50_000 * 0.0495, abs=0.01)


def test_pa_flat_rate():
    rs = load_ruleset(2026, state="pa")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=10_000.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    # PA: flat 3.07% on (st + lt) = 60k * 0.0307
    assert result.state_tax_usd == pytest.approx(60_000 * 0.0307, abs=0.01)


# ----- MA preferential split -------------------------------------------------


def test_ma_stcg_ltcg_split():
    """MA: STCG 8.5%, LTCG 5.0%. $20k ST + $30k LT = $1,700 + $1,500 = $3,200."""
    rs = load_ruleset(2026, state="ma")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=20_000.0,
        lt_cap_gain_usd=30_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.state_tax_usd == pytest.approx(3_200.00, abs=0.01)


# ----- NJ no Schedule D ------------------------------------------------------


def test_nj_treats_st_lt_as_ordinary():
    """NJ: STCG/LTCG indistinguishable; both at NJ brackets."""
    rs = load_ruleset(2026, state="nj")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=10_000.0,
        lt_cap_gain_usd=20_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    # The total $30k cap-gain stacked on $80k ordinary:
    # NJ single brackets: 75k-500k at 6.37%. From 80k-110k = 30k * 6.37% = 1,911.
    assert result.state_tax_usd == pytest.approx(1_911.00, abs=2.0)


# ----- Negative gain (loss) clamps to zero -----------------------------------


def test_loss_clamps_to_zero_no_negative_tax():
    rs = load_ruleset(2026, state="ca")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=-5_000.0,
        lt_cap_gain_usd=-2_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.federal_ordinary_tax_usd == 0.0
    assert result.federal_ltcg_tax_usd == 0.0
    assert result.niit_usd == 0.0
    assert result.state_tax_usd == 0.0
    assert result.total_estimated_tax_usd == 0.0


# ----- Smoke: every shipped state runs to completion -------------------------


_V12_STATES = [
    "ca", "ny", "tx", "fl", "nj", "il", "ma", "pa", "ga", "nc",
    "wa", "ak", "nv", "nh", "sd", "tn", "wy",
]


@pytest.mark.parametrize("state_code", _V12_STATES)
def test_load_and_compute_all_v12_states(state_code):
    ruleset = load_ruleset(2026, state=state_code)
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=50_000.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=80_000.0,
        ruleset=ruleset,
        filing_status="single",
    )
    assert result.total_estimated_tax_usd >= 0
    assert 0.0 <= result.effective_marginal_rate <= 1.0


# ----- Filing status validation ----------------------------------------------


def test_invalid_filing_status_raises():
    rs = load_ruleset(2026)
    with pytest.raises(ValueError, match="filing_status"):
        compute_jurisdiction_tax(
            st_cap_gain_usd=0.0,
            lt_cap_gain_usd=50_000.0,
            ordinary_income_usd=0.0,
            ruleset=rs,
            filing_status="bogus",
        )


# ----- Disclaimer + fallback years -------------------------------------------


def test_disclaimer_set_when_fallback_years_present():
    # 2030 file does not exist; falls back to 2026 → disclaimer set.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        rs = load_ruleset(2030, state="ca")
    result = compute_jurisdiction_tax(
        st_cap_gain_usd=0.0,
        lt_cap_gain_usd=50_000.0,
        ordinary_income_usd=0.0,
        ruleset=rs,
        filing_status="single",
    )
    assert result.disclaimer is not None
    assert "2030" in result.disclaimer
    assert result.used_fallback_years
