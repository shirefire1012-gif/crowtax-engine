"""Ordinary-income acquisitions: mining, staking, airdrops, forks.

Roadmap item 1.5.  Non-purchase crypto acquisitions are ordinary income
at FMV on the date of receipt under:

    * Notice 2014-21 Q-8            (mining)
    * Rev. Rul. 2023-14             (staking - income on dominion/control)
    * Rev. Rul. 2019-24             (airdrops and hard forks)

The basis of the acquired coins equals that receipt-date FMV, so a later
disposition produces capital gain only on appreciation *after* receipt.
The income itself flows through Schedule 1 (line 8v for digital-asset
income, or the successor line number on the 2026 form) separately from
the capital-gain chain on Form 8949 / Schedule D.

Design:

    * ``record_income()`` inserts one ``tax_ordinary_income`` row per
      non-purchase acquisition.  Callers pass FMV either directly or
      accept the default $1.00 stablecoin peg.  If no FMV can be
      established, the row is written with ``fmv_usd=0`` and
      ``needs_review=TRUE`` - it never silently defaults to zero.
    * ``recognize_for_lot()`` is the integration hook: given a freshly
      promoted ``tax_lots`` row with ``acquisition_type`` in the
      ordinary-income set, it writes the income row and links it back
      via ``tax_ordinary_income.tax_lot_id``.  The lot's
      ``cost_basis_usd`` / ``cost_basis_per_unit`` are updated to the
      FMV (they may have been written from ``price_usd=0`` at ingest
      time for airdrops, etc.).
    * ``summarize_by_year_and_type()`` is the report-side aggregator
      that Schedule 1 output consumes (item 1.7).

Kept deliberately small.  The FMV oracle integration (item 1.5
acceptance criterion 4) lives in the ingest paths; this module only
records what the caller supplies and flags zero-FMV as review-needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import psycopg2.extras

log = logging.getLogger(__name__)

ORDINARY_INCOME_TYPES = frozenset(("mining", "staking", "airdrop", "fork", "lp_fees"))

STABLECOIN_SYMBOLS = frozenset((
    "USDC", "USDT", "DAI", "USDP", "GUSD", "BUSD", "PYUSD", "FDUSD",
))


def _epoch_year(epoch_seconds: int) -> int:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).year


def record_income(
    conn,
    *,
    income_type: str,
    symbol: str,
    quantity,
    received_at: int,
    fmv_usd: Optional[Decimal] = None,
    fmv_source: Optional[str] = None,
    account_id: Optional[int] = None,
    tax_lot_id: Optional[int] = None,
    raw_transaction_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> int:
    """Insert an ordinary-income row.  Returns the new id.

    FMV resolution order:
        1. Explicit ``fmv_usd`` from the caller (preferred).
        2. Stablecoin peg: $1.00 * quantity when symbol is in
           ``STABLECOIN_SYMBOLS`` and ``fmv_usd`` is None.
        3. Fallback: ``fmv_usd=0`` and ``needs_review=TRUE``.
    """
    if income_type not in ORDINARY_INCOME_TYPES:
        raise ValueError(
            f"income_type {income_type!r} not in "
            f"{sorted(ORDINARY_INCOME_TYPES)}"
        )

    quantity = Decimal(str(quantity))
    needs_review = False
    if fmv_usd is None:
        if symbol.upper() in STABLECOIN_SYMBOLS:
            fmv_usd = quantity  # 1 stable = $1
            fmv_source = fmv_source or "stablecoin_par"
        else:
            fmv_usd = Decimal(0)
            needs_review = True
            fmv_source = fmv_source or "missing"
    else:
        fmv_usd = Decimal(str(fmv_usd))
        fmv_source = fmv_source or "caller"

    fmv_per_unit = (fmv_usd / quantity) if quantity > 0 else Decimal(0)

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_ordinary_income
                (account_id, symbol, received_at, quantity,
                 fmv_usd, fmv_per_unit, fmv_source, income_type,
                 tax_lot_id, raw_transaction_id, needs_review, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (account_id, symbol, received_at, quantity,
             fmv_usd, fmv_per_unit, fmv_source, income_type,
             tax_lot_id, raw_transaction_id, needs_review, notes),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def recognize_for_lot(
    conn,
    lot_id: int,
    *,
    fmv_per_unit: Optional[Decimal] = None,
    fmv_source: Optional[str] = None,
) -> Optional[int]:
    """Write an ordinary-income row for an already-promoted lot.

    Reads the lot, ensures its ``acquisition_type`` is in
    ``ORDINARY_INCOME_TYPES``, and inserts the corresponding
    ``tax_ordinary_income`` row.  Also rewrites the lot's
    ``cost_basis_usd`` / ``cost_basis_per_unit`` to the FMV so the
    Schedule D side produces gain only on appreciation after receipt.

    Returns the new income-row id, or ``None`` if the lot's
    acquisition_type does not qualify.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM tax_lots WHERE id = %s", (lot_id,))
        lot = cur.fetchone()
        if lot is None:
            raise ValueError(f"Lot {lot_id} not found")

        acq_type = lot["acquisition_type"]
        if acq_type not in ORDINARY_INCOME_TYPES:
            return None

        quantity = Decimal(str(lot["quantity"]))
        if fmv_per_unit is None:
            # Use whatever basis the lot already has - that will have
            # been the spot price from the ingest path or 0 for illiquid
            # airdrops.
            fmv_per_unit = Decimal(str(lot["cost_basis_per_unit"]))
            fmv_source = fmv_source or "lot_basis_inherited"
        fmv_per_unit = Decimal(str(fmv_per_unit))
        fmv_usd = fmv_per_unit * quantity

        new_id = record_income(
            conn,
            income_type=acq_type,
            symbol=lot["symbol"],
            quantity=quantity,
            received_at=lot["acquired_at"],
            fmv_usd=fmv_usd,
            fmv_source=fmv_source,
            account_id=lot["account_id"],
            tax_lot_id=lot_id,
            raw_transaction_id=lot.get("raw_transaction_id"),
        )

        # Keep the lot's basis in lockstep with the recognised income.
        cur2 = conn.cursor()
        try:
            cur2.execute(
                """
                UPDATE tax_lots
                SET cost_basis_usd = %s,
                    cost_basis_per_unit = %s
                WHERE id = %s
                """,
                (fmv_usd, fmv_per_unit, lot_id),
            )
            conn.commit()
        finally:
            cur2.close()
        return new_id
    finally:
        cur.close()


def summarize_by_year_and_type(conn, year: Optional[int] = None) -> dict:
    """Aggregate ordinary-income totals for Schedule 1.

    Returns a nested dict::

        {
            2024: {"mining": Decimal, "staking": Decimal, ...},
            2025: {...},
        }

    When ``year`` is provided, returns only that year's sub-dict.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT income_type, received_at, fmv_usd
            FROM tax_ordinary_income
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    out: dict[int, dict[str, Decimal]] = {}
    for income_type, received_at, fmv_usd in rows:
        yr = _epoch_year(received_at)
        out.setdefault(yr, {})
        out[yr].setdefault(income_type, Decimal(0))
        out[yr][income_type] += Decimal(str(fmv_usd))

    if year is not None:
        return out.get(year, {})
    return out


def list_review_queue(conn) -> list[dict]:
    """Return all ordinary-income rows that need manual FMV review."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT * FROM tax_ordinary_income
            WHERE needs_review = TRUE
            ORDER BY received_at ASC, id ASC
            """
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
