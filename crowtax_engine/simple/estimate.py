"""Pure-function tax estimator. Stub for now; impl in subsequent tasks."""

from __future__ import annotations

from decimal import Decimal

from crowtax_engine.simple.types import (
    SimpleEstimateInput,
    SimpleEstimateResult,
)


def estimate(inp: SimpleEstimateInput) -> SimpleEstimateResult:
    """Compute estimated total tax. Stub returns zeros."""
    return SimpleEstimateResult(
        tax_year=inp.tax_year,
        total_tax_usd=Decimal(0),
        federal_stcg_usd=Decimal(0),
        federal_ltcg_usd=Decimal(0),
        niit_usd=Decimal(0),
        state_usd=Decimal(0),
        city_usd=Decimal(0),
        after_tax_delta_usd=Decimal(0),
        state_supported=False,
        used_top_bracket_fallback=False,
    )
