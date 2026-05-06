"""Dataclass types for crowtax_engine.simple.estimate()."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class SimpleEstimateInput:
    """User-supplied inputs to the simple-mode estimator.

    All monetary fields are USD. ``other_income_usd=None`` triggers the
    top-bracket fallback (37% federal on STCG/LTCG marginal slot).

    ``holding_period`` controls how the gain is split:
    - ``"short"`` → all STCG
    - ``"long"`` → all LTCG
    - ``"mixed"`` → split per ``mixed_split_short_pct`` (default 50/50).
    """

    tax_year: int
    filing_status: str         # 'single' | 'mfj' | 'mfs' | 'hoh'
    state: str                 # ISO state abbreviation, e.g., 'CA' (case-insensitive)
    city: str | None           # 'new_york_city', 'philadelphia', None
    proceeds_usd: Decimal
    cost_basis_usd: Decimal
    holding_period: str        # 'short' | 'long' | 'mixed'
    mixed_split_short_pct: int = 50
    other_income_usd: Decimal | None = None


@dataclass(frozen=True)
class SimpleEstimateResult:
    """Computed tax breakdown for one simple-mode submission.

    All amounts in USD. ``state_supported=False`` indicates the state's
    YAML wasn't found and the estimate covers federal-only;
    ``state_usd`` will be 0 in that case. ``used_top_bracket_fallback``
    indicates the user didn't supply ``other_income_usd`` and the
    federal STCG figure is the top-bracket worst-case.
    """

    tax_year: int
    total_tax_usd: Decimal
    federal_stcg_usd: Decimal
    federal_ltcg_usd: Decimal
    niit_usd: Decimal
    state_usd: Decimal
    city_usd: Decimal
    after_tax_delta_usd: Decimal
    state_supported: bool
    used_top_bracket_fallback: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
