"""Simple-mode tax estimator (no exchange data, just user-entered numbers).

Public, stateless function for the funnel-friendly `/simple` page. Takes
five inputs (year, filing status, jurisdiction, proceeds, basis) plus an
optional AGI, returns a federal+state+city tax estimate.

Wraps the existing `compute_jurisdiction_tax` engine machinery to keep
the math in one place — the simple-mode estimator is a UX shim around
the same brackets and rules that drive the dashboard KPI card.
"""

from crowtax_engine.simple.estimate import estimate
from crowtax_engine.simple.types import (
    SimpleEstimateInput,
    SimpleEstimateResult,
)

__all__ = ["estimate", "SimpleEstimateInput", "SimpleEstimateResult"]
