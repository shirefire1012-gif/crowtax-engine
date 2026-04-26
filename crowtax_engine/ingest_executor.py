"""Sync executor trades from exit_log into the tax engine."""

import logging
from decimal import Decimal

import psycopg2.extras

from crowtax_engine.db import PONYBOY_DSN, get_conn
from crowtax_engine.staging import ingest_raw, promote_confirmed

log = logging.getLogger(__name__)

# Map exchange name to chain
EXCHANGE_CHAIN = {
    "hyperliquid": "HYPE",
    "paradex": "ETH",
    "lighter": "ETH",
    "nado": "ETH",
}

# Taker fee rate used when we have to ESTIMATE. These are rough, per-exchange
# defaults (taker side — the conservative assumption for aggressive entries).
# Real values should always come from exchange_data_raw when available; these
# are only used to avoid recording fee_usd = 0 when we know better.
EXCHANGE_TAKER_FEE_BPS = {
    "hyperliquid": Decimal("2.5"),   # 0.025%
    "paradex": Decimal("5.0"),       # 0.05%
    "lighter": Decimal("2.0"),       # 0.02%
    "nado": Decimal("5.0"),          # 0.05%
}


def _exchange_data_raw_exists(conn) -> bool:
    """Return True iff the exchange_data_raw table exists in the current DB.

    We guard every real-fee lookup with this so the tax sync never blocks
    when the actual-fee capture plumbing hasn't shipped yet. The check is
    read-only and cheap.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.exchange_data_raw') IS NOT NULL")
        return bool(cur.fetchone()[0])
    except Exception:
        return False
    finally:
        cur.close()


def _lookup_real_fee(conn, exchange: str, symbol: str, ts: int) -> Decimal | None:
    """Return actual fee_usd from exchange_data_raw, or None if unavailable.

    Matches on (exchange, symbol, trade_ts) within a small window so
    rounding in the timestamp doesn't miss the trade. Exceptions (column
    missing, table renamed, etc.) propagate up to the caller as None — we
    never want a tax sync to fail because the fee table drifted.
    """
    cur = conn.cursor()
    try:
        # Window of ± 5s around the executor's recorded timestamp.
        cur.execute(
            """
            SELECT fee_usd FROM exchange_data_raw
            WHERE exchange = %s
              AND symbol = %s
              AND ts BETWEEN %s AND %s
            ORDER BY ABS(ts - %s) ASC
            LIMIT 1
            """,
            (exchange, symbol, ts - 5, ts + 5, ts),
        )
        row = cur.fetchone()
        return Decimal(str(row[0])) if row and row[0] is not None else None
    except Exception as e:
        log.debug("real-fee lookup failed (%s/%s): %s", exchange, symbol, e)
        return None
    finally:
        cur.close()


def _estimate_fee(exchange: str, notional_usd: Decimal) -> Decimal:
    """Estimate fee_usd from exchange's default taker rate."""
    bps = EXCHANGE_TAKER_FEE_BPS.get(exchange, Decimal("5.0"))  # 0.05% default
    return (notional_usd * bps / Decimal("10000")).quantize(Decimal("0.000001"))


def _resolve_fee(conn, exchange: str, symbol: str, ts: int,
                 notional_usd: Decimal, real_fees_available: bool) -> tuple[Decimal, str]:
    """Return (fee_usd, fee_source) where fee_source is 'actual' or 'estimated'."""
    if real_fees_available:
        real = _lookup_real_fee(conn, exchange, symbol, ts)
        if real is not None:
            return real, "actual"
    return _estimate_fee(exchange, notional_usd), "estimated"


def sync_executor_trades(conn):
    """Read exit_log rows not yet ingested and create raw transactions.

    Idempotent via source_tx_id dedup. Fee data preference:
      1. Actual fees from ``exchange_data_raw`` when that table exists and
         carries a matching row — recorded as ``fee_source='actual'``.
      2. Estimated fees from ``EXCHANGE_TAKER_FEE_BPS`` — recorded as
         ``fee_source='estimated'`` so tax reports can surface the
         distinction rather than silently treat both as ground truth.

    The caller owns the connection's autocommit/isolation mode.
    """
    read_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Check once per sync whether real fees are available at all.
    real_fees_available = _exchange_data_raw_exists(conn)
    if not real_fees_available:
        log.info(
            "exchange_data_raw not present — executor trades will use "
            "estimated fees (fee_source='estimated')"
        )

    try:
        # Get all exit_log rows
        read_cur.execute("""
            SELECT * FROM exit_log ORDER BY ts ASC
        """)
        rows = read_cur.fetchall()
        ingested = 0

        for row in rows:
            source_tx_id = f"executor:{row['id']}"
            chain = EXCHANGE_CHAIN.get(row["exchange"], "ETH")
            exchange = row["exchange"]
            symbol = row["symbol"]

            # Check if already ingested
            read_cur.execute("""
                SELECT id FROM tax_raw_transactions
                WHERE source = 'executor'
                  AND raw_json->>'source_tx_id' = %s
            """, (source_tx_id + ":entry",))
            if read_cur.fetchone():
                continue

            notional = Decimal(str(row["notional_usd"])) if row["notional_usd"] else Decimal(0)
            entry_price = Decimal(str(row["entry_price"])) if row["entry_price"] else Decimal(0)
            exit_price = Decimal(str(row["exit_price"])) if row["exit_price"] else Decimal(0)
            quantity = (notional / entry_price) if entry_price > 0 and notional > 0 else Decimal(0)

            entry_fee, entry_fee_source = _resolve_fee(
                conn, exchange, symbol, row["entry_ts"], notional, real_fees_available
            )
            exit_notional = (quantity * exit_price) if quantity > 0 else notional
            exit_fee, exit_fee_source = _resolve_fee(
                conn, exchange, symbol, row["ts"], exit_notional, real_fees_available
            )

            # Build raw_json for the entry side (acquisition)
            entry_json = {
                "source": "executor",
                "source_tx_id": source_tx_id + ":entry",
                "type": "buy",
                "exchange": exchange,
                "symbol": symbol,
                "quantity": str(quantity),
                "price_usd": str(entry_price),
                "fee_usd": str(entry_fee),
                "fee_source": entry_fee_source,
                "wallet_address": None,
                "chain": chain,
                "entry_ts": row["entry_ts"],
                "leverage": str(row["leverage"]) if row["leverage"] else None,
            }

            # Build raw_json for the exit side (disposal)
            exit_json = {
                "source": "executor",
                "source_tx_id": source_tx_id + ":exit",
                "type": "sell",
                "exchange": exchange,
                "symbol": symbol,
                "quantity": str(quantity),
                "price_usd": str(exit_price),
                "fee_usd": str(exit_fee),
                "fee_source": exit_fee_source,
                "wallet_address": None,
                "chain": chain,
                "ts": row["ts"],
            }

            # Ingest entry (acquisition)
            ingest_raw(conn, source="executor", chain=chain,
                       timestamp=row["entry_ts"], raw_json=entry_json,
                       status="confirmed")

            # Ingest exit (disposal)
            ingest_raw(conn, source="executor", chain=chain,
                       timestamp=row["ts"], raw_json=exit_json,
                       status="confirmed")

            ingested += 1

        # Promote all confirmed
        promote_confirmed(conn)

        log.info("Synced %d executor trades", ingested)
        return ingested

    except Exception:
        conn.rollback()
        raise
    finally:
        read_cur.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = get_conn(PONYBOY_DSN)
    conn.autocommit = False
    try:
        count = sync_executor_trades(conn)
        print(f"Synced {count} executor trades")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
