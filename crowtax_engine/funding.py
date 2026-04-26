"""Perpetual funding events as ordinary income / expense.

Roadmap item 1.6.  Funding payments on HL / Bybit / OKX / EdgeX perps
have no IRS primary guidance; the engine follows the practitioner
consensus (CoinTracker, TokenTax, Green Trader Tax, Awaken) of treating
each payment as an ordinary-income (received) or ordinary-expense
(paid) event at time of payment.  Funding received in USDC is IRC
section 61 gross income valued at FMV; USDC at par = $1 per unit.

This is uncertain ground - document in DECISIONS.md and retain the
ability to reclassify funding as basis-of-position if a CPA prefers.

Integration pattern:

    1. Executor / CSV ingest writes a ``tax_funding_events`` row for
       every funding payment.
    2. If the settlement creates a spot USDC balance on the exchange
       (typical for HL), the ingest path ALSO creates a ``tax_lots``
       row for the USDC at basis = FMV (which is the same dollar
       amount) and links it via ``tax_funding_events.tax_lot_id``.
       Subsequent USDC disposal produces zero capital gain (basis
       equals par), so the funding income is not double-counted.
    3. ``tax/report.py`` aggregates by ``direction`` and year for
       Schedule 1 output (item 1.7).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

log = logging.getLogger(__name__)

VALID_DIRECTIONS = frozenset(("received", "paid"))


def _epoch_year(epoch_seconds: int) -> int:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).year


def record_funding(
    conn,
    *,
    account_id: Optional[int],
    symbol_perp: str,
    funding_at: int,
    funding_usd,
    settlement_symbol: Optional[str] = None,
    raw_transaction_id: Optional[int] = None,
    tax_lot_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> int:
    """Insert a ``tax_funding_events`` row.  Returns the new id.

    ``funding_usd`` is signed: positive = received by the taxpayer,
    negative = paid by the taxpayer.  The ``direction`` column is
    derived from the sign, not passed separately, so caller mistakes
    cannot produce a mismatch.

    A zero-dollar funding event is legal (protocols sometimes emit
    one on position open); it is recorded with direction='received'
    and is a no-op on the Schedule 1 totals.
    """
    funding_usd = Decimal(str(funding_usd))
    direction = "paid" if funding_usd < 0 else "received"

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_funding_events
                (account_id, symbol_perp, funding_at, funding_usd,
                 direction, settlement_symbol, raw_transaction_id,
                 tax_lot_id, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (account_id, symbol_perp, funding_at, funding_usd,
             direction, settlement_symbol, raw_transaction_id,
             tax_lot_id, notes),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def summarize_by_year(conn, year: Optional[int] = None) -> dict:
    """Aggregate funding totals for Schedule 1.

    Returns a nested dict::

        {
            2024: {"received": Decimal, "paid": Decimal, "net": Decimal},
            2025: {...},
        }

    ``paid`` values are stored negative in the database and preserved
    here so the Schedule 1 deduction line carries the correct sign.
    ``net`` = received + paid (so paid reduces net).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT direction, funding_at, funding_usd
            FROM tax_funding_events
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    out: dict[int, dict[str, Decimal]] = {}
    for direction, funding_at, funding_usd in rows:
        yr = _epoch_year(funding_at)
        bucket = out.setdefault(yr, {
            "received": Decimal(0),
            "paid": Decimal(0),
            "net": Decimal(0),
        })
        bucket[direction] += Decimal(str(funding_usd))
        bucket["net"] += Decimal(str(funding_usd))

    if year is not None:
        return out.get(year, {
            "received": Decimal(0),
            "paid": Decimal(0),
            "net": Decimal(0),
        })
    return out
