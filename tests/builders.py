"""Synthetic row builders for tax engine tests.

All builders insert rows directly into the test DB, bypassing the
staging pipeline. They return the assigned primary-key id so tests can
re-query or chain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


def _epoch(s: str) -> int:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS' to epoch seconds (UTC)."""
    fmts = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            return int(
                datetime.strptime(s, fmt)
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            continue
    raise ValueError(f"bad date string {s!r}")


def make_account(
    conn,
    *,
    source: str = "test",
    wallet_address: str = "0xTEST",
    chain: str = "ETH",
    display_name: str | None = None,
) -> int:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_accounts
                (source, wallet_address, chain, display_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, wallet_address, chain) DO UPDATE
                SET display_name = COALESCE(
                    EXCLUDED.display_name, tax_accounts.display_name)
            RETURNING id
            """,
            (source, wallet_address, chain,
             display_name or f"{source}:{wallet_address}"),
        )
        account_id = cur.fetchone()[0]
        conn.commit()
        return account_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def make_lot(
    conn,
    *,
    symbol: str,
    quantity,
    price_usd,
    acquired_at,
    account_id: int | None = None,
    wallet_address: str | None = None,
    chain: str = "ETH",
    fee_usd=0,
    acquisition_type: str = "purchase",
    source: str = "test",
    source_tx_id: str | None = None,
    asset_class: str = "fungible",
) -> int:
    """Insert a lot with fee-inclusive basis (cost_basis = qty*price + fee).

    ``asset_class`` defaults to ``'fungible'``; pass ``'nft_collectible'``
    or ``'nft_non_collectible'`` to exercise the roadmap-2.4 path.
    """
    if isinstance(acquired_at, str):
        acquired_at = _epoch(acquired_at)
    quantity = Decimal(str(quantity))
    price_usd = Decimal(str(price_usd))
    fee_usd = Decimal(str(fee_usd))

    cost_basis = quantity * price_usd + fee_usd
    cost_per_unit = (cost_basis / quantity) if quantity > 0 else Decimal(0)

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_lots
                (account_id, wallet_address, chain, symbol, acquired_at,
                 quantity, cost_basis_usd, cost_basis_per_unit,
                 remaining_quantity, acquisition_type, fee_usd,
                 source, source_tx_id, asset_class)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (account_id, wallet_address, chain, symbol, acquired_at,
             quantity, cost_basis, cost_per_unit, quantity,
             acquisition_type, fee_usd, source, source_tx_id,
             asset_class),
        )
        lot_id = cur.fetchone()[0]
        conn.commit()
        return lot_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def make_disposal(
    conn,
    *,
    symbol: str,
    quantity,
    proceeds_usd,
    disposed_at,
    account_id: int | None = None,
    wallet_address: str | None = None,
    chain: str = "ETH",
    fee_usd=0,
    source: str = "test",
    source_tx_id: str | None = None,
) -> int:
    """Insert a disposal row. Proceeds are stored GROSS; the engine subtracts
    the fee at match time (post item 1.3)."""
    if isinstance(disposed_at, str):
        disposed_at = _epoch(disposed_at)
    quantity = Decimal(str(quantity))
    proceeds_usd = Decimal(str(proceeds_usd))
    fee_usd = Decimal(str(fee_usd))

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_disposals
                (account_id, wallet_address, chain, symbol, disposed_at,
                 quantity, proceeds_usd, fee_usd, source, source_tx_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (account_id, wallet_address, chain, symbol, disposed_at,
             quantity, proceeds_usd, fee_usd, source, source_tx_id),
        )
        disposal_id = cur.fetchone()[0]
        conn.commit()
        return disposal_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
