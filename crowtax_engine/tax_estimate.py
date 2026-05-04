"""
Jurisdiction-aware tax estimate.

This module computes an estimated tax bill from short-term and long-term
capital gain figures plus an optional ordinary-income approximation, the
user's filing status, and a fully-resolved ``EffectiveRuleset`` from
``jurisdictions.loader``.

The single public entry point is :func:`compute_jurisdiction_tax`. It is
called from ``server/src/crowtax/api/dashboard.py`` to populate the
``estimated_tax_owed_usd`` KPI card and its breakdown, replacing the
former hardcoded 24%/15% short/long approximation.

This file is intentionally pure-Python with no external dependencies
beyond ``jurisdictions.loader`` from the same package — the engine
remains drop-in usable for any analytic dashboard or third-party
consumer without pulling in server concerns.
"""

from __future__ import annotations

import dataclasses
from typing import Iterable

from jurisdictions.loader import EffectiveRuleset

__all__ = ["JurisdictionTaxResult", "compute_jurisdiction_tax"]


# ----- Result dataclass ------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class JurisdictionTaxResult:
    """All-in-one envelope for the dashboard KPI card.

    All amounts in USD. ``effective_marginal_rate`` is the average rate
    applied to (st + lt) capital gains, exposed for "days to LT
    eligibility" tax-savings calculations.
    """

    federal_ordinary_tax_usd: float
    federal_ltcg_tax_usd: float
    niit_usd: float
    state_tax_usd: float
    city_tax_usd: float
    total_estimated_tax_usd: float
    state_taxable_cap_gain_usd: float
    effective_marginal_rate: float
    used_fallback_years: dict[str, int]
    disclaimer: str | None


# ----- Bracket math ----------------------------------------------------------


def _filter_brackets(
    brackets: Iterable[dict],
    filing_status: str,
) -> list[dict]:
    """Return brackets for ``filing_status`` sorted by threshold ascending."""
    rows = [b for b in brackets if b.get("filing_status") == filing_status]
    rows.sort(key=lambda b: b["threshold"])
    return rows


def _apply_bracket_tax(
    income: float,
    brackets: list[dict] | None,
    filing_status: str,
) -> float:
    """
    Compute total marginal-bracket tax on ``income`` for ``filing_status``.

    ``brackets`` is the per-jurisdiction list of
    ``{filing_status, threshold, rate}`` dicts. If ``None`` or empty,
    returns 0. Returns the *total* tax, not the marginal-on-top-dollar.
    """
    if income <= 0 or not brackets:
        return 0.0
    rows = _filter_brackets(brackets, filing_status)
    if not rows:
        return 0.0

    tax = 0.0
    for i, row in enumerate(rows):
        lower = row["threshold"]
        rate = row["rate"]
        upper = rows[i + 1]["threshold"] if i + 1 < len(rows) else float("inf")
        if income <= lower:
            break
        slice_top = min(income, upper)
        tax += (slice_top - lower) * rate
        if income <= upper:
            break
    return tax


def _apply_marginal_on_top_layer(
    base_income: float,
    layer_amount: float,
    brackets: list[dict] | None,
    filing_status: str,
) -> float:
    """
    Compute tax on ``layer_amount`` stacked on top of ``base_income``.

    Used when the cap gain sits on top of ordinary income for state /
    federal-ordinary marginal allocation. Returns the incremental tax
    from the ``layer_amount`` portion.
    """
    if layer_amount <= 0 or not brackets:
        return 0.0
    total_with = _apply_bracket_tax(
        base_income + layer_amount, brackets, filing_status
    )
    total_without = _apply_bracket_tax(
        base_income, brackets, filing_status
    )
    return max(0.0, total_with - total_without)


# ----- Federal ---------------------------------------------------------------


def _compute_federal(
    st: float,
    lt: float,
    ordinary_income: float,
    federal_layer: dict,
    filing_status: str,
) -> tuple[float, float, float]:
    """Returns (federal_ordinary_tax, federal_ltcg_tax, niit)."""
    ordinary_brackets = federal_layer.get("ordinary_brackets")
    ltcg_brackets = federal_layer.get("ltcg_brackets")

    # ST + ordinary income are taxed at the ordinary brackets. We compute
    # the total ordinary tax, then take the marginal on the ST portion
    # for clean breakdown reporting.
    federal_ordinary = _apply_marginal_on_top_layer(
        ordinary_income, max(0.0, st), ordinary_brackets, filing_status
    )

    # LTCG sits on top of ordinary income for threshold purposes per
    # IRC §1(h). Use the ordinary-income base for bracket lookup.
    federal_ltcg = _apply_marginal_on_top_layer(
        ordinary_income, max(0.0, lt), ltcg_brackets, filing_status
    )

    # NIIT: 3.8% on lesser of (net investment income) or (MAGI - threshold)
    niit = 0.0
    if federal_layer.get("niit_applies"):
        niit_rate = federal_layer.get("niit_rate", 0.038)
        thresholds = federal_layer.get("niit_thresholds", {})
        threshold = thresholds.get(filing_status, 200_000)
        magi = ordinary_income + max(0.0, st) + max(0.0, lt)
        net_investment_income = max(0.0, st) + max(0.0, lt)
        excess_over_threshold = max(0.0, magi - threshold)
        niit = min(net_investment_income, excess_over_threshold) * niit_rate

    return federal_ordinary, federal_ltcg, niit


# ----- State -----------------------------------------------------------------


def _compute_wa_excise(lt: float, excise_block: dict) -> float:
    """WA capital gains excise: 7% of LTCG over $262k threshold."""
    if not excise_block:
        return 0.0
    if excise_block.get("applies_to") != "long_term_only":
        # Future-proof; today only long_term_only is published.
        pass
    threshold = excise_block.get("threshold", 0)
    rate = excise_block.get("rate", 0.0)
    taxable = max(0.0, lt - threshold)
    return taxable * rate


def _compute_state(
    st: float,
    lt: float,
    ordinary_income: float,
    ruleset: EffectiveRuleset,
    filing_status: str,
) -> tuple[float, float]:
    """
    Returns (state_tax_usd, state_taxable_cap_gain_usd).

    ``state_taxable_cap_gain_usd`` is the cap-gain figure *after* any
    state-specific exclusion or subtraction has been applied, exposed
    for the breakdown card.
    """
    eff = ruleset.effective

    if eff.get("no_income_tax") and not eff.get("is_excise_tax"):
        return 0.0, 0.0

    treatment = eff["capital_gains_treatment"]

    # WA excise: only LTCG above the threshold; no state ordinary tax.
    if treatment == "excise":
        excise_block = eff.get("capital_gains_excise") or {}
        excise_tax = _compute_wa_excise(max(0.0, lt), excise_block)
        # State-taxable cap gain (post-deduction) for breakdown display.
        threshold = excise_block.get("threshold", 0)
        state_taxable = max(0.0, max(0.0, lt) - threshold)
        return excise_tax, state_taxable

    if treatment == "none":
        return 0.0, 0.0

    state_layer = ruleset.state_layer or {}

    # Determine state-taxable cap gain (after state-specific
    # exclusion / subtraction) before applying ordinary or preferential
    # rate logic. This is the figure shown to users as
    # "state-taxable capital gain" on the breakdown card.
    raw_total = max(0.0, st) + max(0.0, lt)

    state_taxable_st = max(0.0, st)
    state_taxable_lt = max(0.0, lt)

    excl = eff.get("capital_gains_exclusion_pct")
    sub = eff.get("capital_gains_subtraction_pct")
    if treatment == "exclusion_pct" and excl is not None:
        state_taxable_lt = state_taxable_lt * (1 - excl)
    elif treatment == "subtraction_pct" and sub is not None:
        state_taxable_lt = state_taxable_lt * (1 - sub)

    state_taxable_total = state_taxable_st + state_taxable_lt

    # Preferential-rate states: a flat LTCG rate keyed by holding period.
    if treatment == "preferential_rate":
        ltcg_rate = eff.get("capital_gains_rate")
        stcg_rate = eff.get("short_term_capital_gains_rate")
        # MA: separate rates by holding period.
        if stcg_rate is not None and ltcg_rate is not None:
            tax = state_taxable_st * stcg_rate + state_taxable_lt * ltcg_rate
            return tax, state_taxable_total
        # HI-style: LTCG flat, STCG ordinary brackets.
        if ltcg_rate is not None:
            stcg_tax = _apply_marginal_on_top_layer(
                ordinary_income,
                state_taxable_st,
                state_layer.get("brackets"),
                filing_status,
            )
            if state_layer.get("flat_rate") is not None:
                stcg_tax = state_taxable_st * state_layer["flat_rate"]
            return stcg_tax + state_taxable_lt * ltcg_rate, state_taxable_total

    # Flat-rate state.
    if state_layer.get("flat_rate") is not None:
        rate = state_layer["flat_rate"]
        return state_taxable_total * rate, state_taxable_total

    # Bracket state (CA, NY, NJ, etc.) — ordinary treatment.
    state_brackets = state_layer.get("brackets")
    state_tax = _apply_marginal_on_top_layer(
        ordinary_income,
        state_taxable_total,
        state_brackets,
        filing_status,
    )
    return state_tax, state_taxable_total


# ----- City ------------------------------------------------------------------


def _compute_city(
    st: float,
    lt: float,
    ordinary_income: float,
    state_tax_usd: float,
    ruleset: EffectiveRuleset,
    filing_status: str,
) -> float:
    if ruleset.city_layer is None:
        return 0.0

    city_layer = ruleset.city_layer

    # Philadelphia: passive cap gains explicitly excluded.
    if city_layer.get("no_cap_gains_tax"):
        return 0.0

    city_type = city_layer.get("city_type")

    # Yonkers-style: surcharge on state tax liability.
    if city_type == "state_tax_surcharge":
        rate = city_layer.get("surcharge_rate", 0.0)
        return max(0.0, state_tax_usd) * rate

    # NYC-style: full income-tax bracket schedule on the cap gain layer
    # stacked on ordinary income.
    if city_type == "income_tax":
        gain = max(0.0, st) + max(0.0, lt)
        if city_layer.get("flat_rate") is not None:
            return gain * city_layer["flat_rate"]
        return _apply_marginal_on_top_layer(
            ordinary_income,
            gain,
            city_layer.get("brackets"),
            filing_status,
        )

    # wage_only or unknown — no tax on passive crypto.
    return 0.0


# ----- Public entry point ----------------------------------------------------


def compute_jurisdiction_tax(
    st_cap_gain_usd: float,
    lt_cap_gain_usd: float,
    ordinary_income_usd: float,
    ruleset: EffectiveRuleset,
    filing_status: str,
) -> JurisdictionTaxResult:
    """
    Compute estimated federal + state + city tax for the user's gain
    profile and jurisdiction.

    Parameters
    ----------
    st_cap_gain_usd : float
        Net short-term capital gain in USD (negative values are clamped
        to 0 — a net loss is no tax owed, not a refund).
    lt_cap_gain_usd : float
        Net long-term capital gain in USD.
    ordinary_income_usd : float
        Approximate non-crypto ordinary income for bracket placement.
        v1.2 defaults this to 0 — see ``state-tax-v1.2-design.md`` §13f.
    ruleset : EffectiveRuleset
        Composed federal/state/city ruleset from ``load_ruleset()``.
    filing_status : str
        One of ``"single"``, ``"mfj"``, ``"mfs"``, ``"hoh"``.

    Returns
    -------
    JurisdictionTaxResult
    """
    if filing_status not in ("single", "mfj", "mfs", "hoh"):
        raise ValueError(
            f"filing_status must be one of single/mfj/mfs/hoh, got {filing_status!r}"
        )

    st = max(0.0, float(st_cap_gain_usd))
    lt = max(0.0, float(lt_cap_gain_usd))
    ordinary_income = max(0.0, float(ordinary_income_usd))

    federal_ordinary, federal_ltcg, niit = _compute_federal(
        st, lt, ordinary_income, ruleset.federal_layer, filing_status
    )
    state_tax, state_taxable = _compute_state(
        st, lt, ordinary_income, ruleset, filing_status
    )
    city_tax = _compute_city(
        st, lt, ordinary_income, state_tax, ruleset, filing_status
    )

    total = federal_ordinary + federal_ltcg + niit + state_tax + city_tax

    total_gain = st + lt
    if total_gain > 0:
        effective_marginal_rate = total / total_gain
    else:
        effective_marginal_rate = 0.0

    # Build a UI-friendly disclaimer for fallback layers.
    disclaimer: str | None = None
    if ruleset.fallback_years:
        layers = ", ".join(
            f"{layer} (using {year} rates)"
            for layer, year in sorted(ruleset.fallback_years.items())
        )
        disclaimer = (
            f"{ruleset.tax_year} rates not yet published for: {layers}. "
            "Estimate uses the most recent available data."
        )

    return JurisdictionTaxResult(
        federal_ordinary_tax_usd=round(federal_ordinary, 2),
        federal_ltcg_tax_usd=round(federal_ltcg, 2),
        niit_usd=round(niit, 2),
        state_tax_usd=round(state_tax, 2),
        city_tax_usd=round(city_tax, 2),
        total_estimated_tax_usd=round(total, 2),
        state_taxable_cap_gain_usd=round(state_taxable, 2),
        effective_marginal_rate=round(effective_marginal_rate, 6),
        used_fallback_years=dict(ruleset.fallback_years),
        disclaimer=disclaimer,
    )
