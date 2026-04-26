"""Asset-family lookup for wrap/stable realization-event handling.


Roadmap item 2.2.  IRS has not issued primary guidance on wraps
(WBTC <-> BTC, WETH <-> ETH, stablecoin <-> stablecoin); Notice 2024-57's
broker-reporting exemption is procedural, not substantive.  The
conservative position adopted here is that every same-family swap is a
realization event reported at FMV.  Gain or loss is typically ~$0 for
on-peg stablecoins, but the line is required - and a depegged-stable
swap (e.g. USDC at $0.95 in a banking crisis) produces a real loss
that must NOT be silently aggregated away.

The lookup table ``tax_asset_families`` is seeded by migration 008.

A swap fill becomes "wrap or stable" only if BOTH legs resolve to the
same family.  Cross-family swaps (USDC <-> ETH, BTC <-> SOL, ...) are
ordinary capital-gain events and are intentionally NOT marked.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Per-process cache.  The seed table is small (<20 rows) and immutable
# during a single tax run; loading once at first call is fine.
_FAMILY_CACHE: Optional[dict[str, str]] = None


def _normalize(symbol: Optional[str]) -> str:
    return (symbol or "").strip().upper()


def _load_cache(conn) -> dict[str, str]:
    cur = conn.cursor()
    try:
        cur.execute("SELECT symbol, family FROM tax_asset_families")
        return {(_normalize(row[0])): row[1] for row in cur.fetchall()}
    finally:
        cur.close()


def get_family(conn, symbol: Optional[str]) -> Optional[str]:
    """Return the family name for ``symbol``, or None if not in any family.

    Lookup is case-insensitive.  Result is cached per process.
    """
    global _FAMILY_CACHE
    if _FAMILY_CACHE is None:
        _FAMILY_CACHE = _load_cache(conn)
    return _FAMILY_CACHE.get(_normalize(symbol))


def reset_cache() -> None:
    """Clear the per-process cache.  Tests reset this between fixtures."""
    global _FAMILY_CACHE
    _FAMILY_CACHE = None


def is_same_family_swap(conn, sell_symbol: Optional[str],
                        buy_symbol: Optional[str]) -> Optional[str]:
    """If both legs are in the same family, return that family name; else None.

    USDC -> USDT             -> 'usd_stable'
    WBTC -> BTC              -> 'btc_wrap'
    ETH  -> USDC             -> None (cross-family)
    USDC -> USDC             -> 'usd_stable' (degenerate but valid)
    """
    fa = get_family(conn, sell_symbol)
    fb = get_family(conn, buy_symbol)
    if fa is not None and fa == fb:
        return fa
    return None


def annotate_lot_disposal_pair(conn, lot_id: Optional[int],
                               disposal_id: Optional[int],
                               family: str) -> None:
    """Mark both rows with ``wrap_family``.  Idempotent."""
    cur = conn.cursor()
    try:
        if lot_id is not None:
            cur.execute(
                "UPDATE tax_lots SET wrap_family = %s WHERE id = %s",
                (family, lot_id),
            )
        if disposal_id is not None:
            cur.execute(
                "UPDATE tax_disposals SET wrap_family = %s WHERE id = %s",
                (family, disposal_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
