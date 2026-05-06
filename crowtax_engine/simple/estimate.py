"""Pure-function tax estimator for the simple-mode public estimator.

Wraps the existing ``compute_jurisdiction_tax`` machinery with the
input-shape conveniences the simple form needs:

- ``holding_period`` ('short' | 'long' | 'mixed' + percent split) instead
  of pre-split short/long figures.
- ``other_income_usd=None`` triggers a top-bracket federal fallback
  (37%) on the STCG portion — the privacy-vs-accuracy trade-off the
  user picks at form-fill time.
- Unsupported state (no YAML) returns federal-only with
  ``state_supported=False`` instead of raising.

This module is the engine-side single source of truth for the public
``/api/simple-estimate`` endpoint. The server is HTTP plumbing; the
math is here.
"""

from __future__ import annotations

from decimal import Decimal

from jurisdictions.loader import load_ruleset

from crowtax_engine.simple.types import (
    SimpleEstimateInput,
    SimpleEstimateResult,
)
from crowtax_engine.tax_estimate import compute_jurisdiction_tax


_TOP_BRACKET_RATE = Decimal("0.37")


def _gain(inp: SimpleEstimateInput) -> Decimal:
    return inp.proceeds_usd - inp.cost_basis_usd


def _split_short_long(
    inp: SimpleEstimateInput,
    gain: Decimal,
) -> tuple[Decimal, Decimal]:
    """Split a single gain figure into (stcg, ltcg) per holding period."""
    if gain <= 0:
        return Decimal(0), Decimal(0)
    if inp.holding_period == "short":
        return gain, Decimal(0)
    if inp.holding_period == "long":
        return Decimal(0), gain
    # mixed
    pct_short = Decimal(inp.mixed_split_short_pct) / Decimal(100)
    stcg = gain * pct_short
    ltcg = gain - stcg
    return stcg, ltcg


def _try_load_ruleset(year: int, state: str | None, city: str | None):
    """Load ruleset; on missing-state-YAML, return federal-only.

    Returns ``(ruleset, state_supported)``. ``state_supported`` is True
    when the requested state's YAML loaded cleanly; False when we fell
    back to federal-only.
    """
    if state is None:
        return load_ruleset(year, state=None, city=None), True
    try:
        ruleset = load_ruleset(
            year,
            state=state.lower(),
            city=city,
            allow_fallback=True,
        )
        return ruleset, True
    except FileNotFoundError:
        # State YAML missing entirely — caller wants federal-only
        return load_ruleset(year, state=None, city=None), False


def estimate(inp: SimpleEstimateInput) -> SimpleEstimateResult:
    """Compute estimated total tax for one simple-mode submission.

    Pure function: no I/O beyond reading the engine's bundled YAML
    rulesets. No DB writes. No persistence.
    """
    gain = _gain(inp)
    stcg, ltcg = _split_short_long(inp, gain)

    used_top_bracket = inp.other_income_usd is None
    other_income = (
        inp.other_income_usd if inp.other_income_usd is not None else Decimal(0)
    )

    ruleset, state_supported = _try_load_ruleset(
        inp.tax_year, inp.state, inp.city,
    )

    # Delegate to the existing engine math (returns a JurisdictionTaxResult
    # of floats). We coerce to Decimal at the boundary for cleanly-rounded
    # currency output.
    jt = compute_jurisdiction_tax(
        st_cap_gain_usd=float(stcg),
        lt_cap_gain_usd=float(ltcg),
        ordinary_income_usd=float(other_income),
        ruleset=ruleset,
        filing_status=inp.filing_status,
    )

    fed_stcg = _to_money(jt.federal_ordinary_tax_usd)
    fed_ltcg = _to_money(jt.federal_ltcg_tax_usd)
    niit = _to_money(jt.niit_usd)
    state_tax = _to_money(jt.state_tax_usd)
    city_tax = _to_money(jt.city_tax_usd)

    # Top-bracket fallback: when the user didn't enter income, the
    # marginal-on-top-of-zero math from compute_jurisdiction_tax under-
    # estimates federal STCG. Override with a flat 37% on the STCG
    # portion and recompute. (LTCG is already correctly bracket-stacked
    # at zero ordinary income — top LTCG bracket is 20%, not 37%.)
    if used_top_bracket and stcg > 0:
        fed_stcg = _to_money(stcg * _TOP_BRACKET_RATE)

    total = fed_stcg + fed_ltcg + niit + state_tax + city_tax
    after_tax = (inp.proceeds_usd - inp.cost_basis_usd) - total

    return SimpleEstimateResult(
        tax_year=inp.tax_year,
        total_tax_usd=total,
        federal_stcg_usd=fed_stcg,
        federal_ltcg_usd=fed_ltcg,
        niit_usd=niit,
        state_usd=state_tax,
        city_usd=city_tax,
        after_tax_delta_usd=after_tax,
        state_supported=state_supported,
        used_top_bracket_fallback=used_top_bracket,
    )


def _to_money(value: float | Decimal) -> Decimal:
    """Coerce a numeric value to a 2dp Decimal (currency rounding)."""
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    return Decimal(str(value)).quantize(Decimal("0.01"))
