"""Worked-example tests for crowtax_engine.simple.estimate()."""

from __future__ import annotations

from decimal import Decimal


def test_module_imports():
    from crowtax_engine.simple import estimate, SimpleEstimateInput  # noqa: F401
    from crowtax_engine.simple import SimpleEstimateResult  # noqa: F401


# ---------- Federal LTCG bracket math ----------


def test_long_term_only_single_filer_low_income():
    """Single, $50k other income, $50k LTCG → 0% bracket on first
    ~$48,350 (0% LTCG cap), then 15% on the remainder.

    With $50k ordinary stacked underneath, the LTCG sits from $50k to
    $100k. The 0% LTCG bracket tops at $48,350 — so the entire LTCG
    is above it, taxed at 15% = $7,500.
    """
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026,
        filing_status="single",
        state="WY",  # no income tax → ignore state
        city=None,
        proceeds_usd=Decimal("50000"),
        cost_basis_usd=Decimal("0"),
        holding_period="long",
        other_income_usd=Decimal("50000"),
    ))
    assert result.federal_ltcg_usd > Decimal(0)
    assert result.federal_stcg_usd == Decimal(0)
    assert result.state_usd == Decimal(0)  # WY = no income tax
    # 50k ordinary stacked under 50k LTCG → entire 50k LTCG sits above
    # the 48350 0%-bracket ceiling → 50k * 15% = 7500.
    assert result.federal_ltcg_usd == Decimal("7500.00")


def test_short_term_uses_ordinary_brackets():
    """STCG with no other income — taxed at marginal ordinary brackets.

    $20k STCG with no other income (AGI=0): brackets cover $20k cleanly:
    - first 11925 @ 10% = 1192.50
    - next (20000-11925=8075) @ 12% = 969.00
    Total = 2161.50.
    """
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026,
        filing_status="single",
        state="WY",
        city=None,
        proceeds_usd=Decimal("30000"),
        cost_basis_usd=Decimal("10000"),
        holding_period="short",
        other_income_usd=Decimal("0"),
    ))
    assert result.federal_stcg_usd == Decimal("2161.50")
    assert result.federal_ltcg_usd == Decimal(0)


def test_top_bracket_fallback_when_no_agi():
    """When other_income is None, federal STCG = 37% of the gain."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026,
        filing_status="single",
        state="WY",
        city=None,
        proceeds_usd=Decimal("10000"),
        cost_basis_usd=Decimal("0"),
        holding_period="short",
        other_income_usd=None,
    ))
    assert result.used_top_bracket_fallback is True
    # 10k at 37% top bracket
    assert result.federal_stcg_usd == Decimal("3700.00")


def test_loss_year_returns_zero_tax():
    """Net loss → 0 federal tax (we don't model carryforwards in v1)."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026,
        filing_status="single",
        state="CA",
        city=None,
        proceeds_usd=Decimal("10000"),
        cost_basis_usd=Decimal("30000"),
        holding_period="long",
        other_income_usd=Decimal("80000"),
    ))
    assert result.federal_ltcg_usd == Decimal(0)
    assert result.federal_stcg_usd == Decimal(0)
    assert result.state_usd == Decimal(0)
    assert result.total_tax_usd == Decimal(0)


# ---------- State coverage ----------


def test_california_state_tax_applied():
    """CA taxes capital gains as ordinary income; brackets are
    progressive. With $100k AGI + $50k LTCG, the LTCG slot in CA's
    brackets sits in the 9.3% range. Expect a positive, plausible
    state tax (roughly $4-5k)."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="CA", city=None,
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("50000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    assert result.state_supported is True
    # CA marginal: 50k slot from 100k to 150k AGI hits the 9.3% bracket
    # (70606 → 360660). Roughly 50k * 9.3% ≈ 4650, but a small slice
    # below 70606 is at 8% — so around 4500-4700.
    assert result.state_usd > Decimal("4000")
    assert result.state_usd < Decimal("5500")


def test_unsupported_state_returns_federal_only():
    """OH has no YAML — estimator returns federal-only with
    state_supported=False."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="OH", city=None,
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("50000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    assert result.state_supported is False
    assert result.state_usd == Decimal(0)
    # Federal still computed
    assert result.federal_ltcg_usd > Decimal(0)


def test_washington_excise_on_large_gain():
    """WA's 7% LTCG excise applies to amounts above the $262k threshold
    in 2026. $400k LTCG → (400k-262k) * 7% = $9,660."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="WA", city=None,
        proceeds_usd=Decimal("500000"), cost_basis_usd=Decimal("100000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    assert result.state_supported is True
    # (400000 - 262000) * 0.07 = 9660
    assert result.state_usd == Decimal("9660.00")


def test_no_income_tax_state_zero_state_tax():
    """WY has no income tax → state_usd is 0 even on a big gain."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="WY", city=None,
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("0"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    assert result.state_supported is True  # WY has YAML; just no income tax
    assert result.state_usd == Decimal(0)


def test_nyc_city_tax_layered_on_ny_state():
    """NYC layers an income-tax bracket on top of NY state."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="NY",
        city="new_york_city",
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("50000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    assert result.state_supported is True
    assert result.state_usd > Decimal(0)
    assert result.city_usd > Decimal(0)


# ---------- Mixed holding-period split ----------


def test_mixed_holding_period_splits_50_50_default():
    """Default mixed split is 50/50 short/long."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="WY", city=None,
        proceeds_usd=Decimal("20000"), cost_basis_usd=Decimal("0"),
        holding_period="mixed",
        other_income_usd=Decimal("100000"),
    ))
    # Both stcg and ltcg should be > 0
    assert result.federal_stcg_usd > Decimal(0)
    assert result.federal_ltcg_usd > Decimal(0)


def test_mixed_holding_period_user_split():
    """80% short / 20% long gives different breakdown than 50/50."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result_80 = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="WY", city=None,
        proceeds_usd=Decimal("20000"), cost_basis_usd=Decimal("0"),
        holding_period="mixed", mixed_split_short_pct=80,
        other_income_usd=Decimal("100000"),
    ))
    result_20 = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="WY", city=None,
        proceeds_usd=Decimal("20000"), cost_basis_usd=Decimal("0"),
        holding_period="mixed", mixed_split_short_pct=20,
        other_income_usd=Decimal("100000"),
    ))
    # 80% short → more STCG than 20% short
    assert result_80.federal_stcg_usd > result_20.federal_stcg_usd
    assert result_80.federal_ltcg_usd < result_20.federal_ltcg_usd


# ---------- After-tax delta + total ----------


def test_after_tax_delta_is_gain_minus_total_tax():
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="WY", city=None,
        proceeds_usd=Decimal("50000"), cost_basis_usd=Decimal("10000"),
        holding_period="long",
        other_income_usd=Decimal("100000"),
    ))
    expected_delta = Decimal("40000") - result.total_tax_usd
    assert result.after_tax_delta_usd == expected_delta


def test_total_is_sum_of_components():
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    result = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="NY",
        city="new_york_city",
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("50000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    expected = (
        result.federal_stcg_usd
        + result.federal_ltcg_usd
        + result.niit_usd
        + result.state_usd
        + result.city_usd
    )
    assert result.total_tax_usd == expected


# ---------- Case-insensitive state ----------


def test_state_case_insensitive():
    """Passing 'ca' or 'CA' gives the same answer."""
    from crowtax_engine.simple import estimate, SimpleEstimateInput
    upper = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="CA", city=None,
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("50000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    lower = estimate(SimpleEstimateInput(
        tax_year=2026, filing_status="single", state="ca", city=None,
        proceeds_usd=Decimal("100000"), cost_basis_usd=Decimal("50000"),
        holding_period="long", other_income_usd=Decimal("100000"),
    ))
    assert upper.state_usd == lower.state_usd
