"""Staging pipeline: ingest → confirm → promote → audit rebuild."""

import json
import logging
from decimal import Decimal

import psycopg2.extras

from crowtax_engine.accounts import get_or_create_account

log = logging.getLogger(__name__)

# Required confirmations per chain
CHAIN_CONFIRMATIONS = {
    "BTC": 6,
    "ETH": 12,
    "INK": 12,
    "SOL": 1,
    "SUI": 1,
    "HYPE": 1,
}

# Transaction types that create lots (acquisitions)
BUY_TYPES = {"buy", "receive", "airdrop", "staking", "mining", "fork", "gift"}
# Transaction types that create disposals
SELL_TYPES = {"sell", "send", "spend"}


def ingest_raw(conn, source: str, chain: str, timestamp: int,
               raw_json: dict, tx_hash: str = None,
               block_number: int = None, source_file: str = None,
               status: str = None, source_tx_id: str = None) -> int:
    """Write a raw transaction to tax_raw_transactions. Returns row ID.

    Dedup is backed by two partial unique indexes (migrations_002):
      - ``(source, tx_hash)`` where ``tx_hash IS NOT NULL``
      - ``(source, raw_json->>'source_tx_id')`` where ``raw_json`` has
        the ``source_tx_id`` key

    So executor / CSV rows (which lack an on-chain tx_hash) are still
    de-duplicated as long as the caller populates ``source_tx_id`` either
    via the keyword arg or inside ``raw_json``. If neither dedup column
    is present we still insert (best-effort) but log a warning.
    """
    if status is None:
        if source in ("executor", "csv"):
            status = "confirmed"
        else:
            status = "pending"

    required = CHAIN_CONFIRMATIONS.get(chain, 1)
    confirmation_count = required if status == "confirmed" else 0

    # Mirror the source_tx_id into raw_json so the dedup index can see it.
    if source_tx_id and "source_tx_id" not in raw_json:
        raw_json = {**raw_json, "source_tx_id": source_tx_id}
    effective_source_tx_id = raw_json.get("source_tx_id") or source_tx_id

    if not tx_hash and not effective_source_tx_id:
        log.warning(
            "ingest_raw: row has neither tx_hash nor source_tx_id — "
            "dedup is best-effort (source=%s chain=%s ts=%s)",
            source, chain, timestamp,
        )

    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO tax_raw_transactions
                (source, source_file, chain, tx_hash, block_number, timestamp,
                 raw_json, status, confirmation_count, required_confirmations)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (source, source_file, chain, tx_hash, block_number, timestamp,
              json.dumps(raw_json), status, confirmation_count, required))
        row = cur.fetchone()
        if row is not None:
            conn.commit()
            return row[0]

        # Duplicate — look up the existing row by whichever dedup key we have.
        if tx_hash:
            cur.execute(
                "SELECT id FROM tax_raw_transactions WHERE source=%s AND tx_hash=%s",
                (source, tx_hash),
            )
            row = cur.fetchone()
        if row is None and effective_source_tx_id:
            cur.execute(
                """
                SELECT id FROM tax_raw_transactions
                WHERE source = %s
                  AND raw_json->>'source_tx_id' = %s
                """,
                (source, effective_source_tx_id),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0] if row is not None else -1
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def confirm_pending(conn):
    """Check confirmation counts for pending transactions.

    Stub: marks all pending as confirmed (real implementation would
    query block explorers for current block height).
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE tax_raw_transactions
            SET status = 'confirmed',
                confirmation_count = required_confirmations
            WHERE status = 'pending'
        """)
        count = cur.rowcount
        conn.commit()
        log.info("Confirmed %d pending transactions", count)
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _parse_acquisition_type(raw_json: dict) -> str:
    """Determine acquisition_type from raw transaction data."""
    tx_type = raw_json.get("type", "").lower()
    if tx_type in ("airdrop", "fork", "staking", "mining", "gift"):
        return tx_type
    if tx_type == "swap":
        return "swap"
    return "purchase"


PROMOTE_BATCH_SIZE = 500


def promote_confirmed(conn, batch_size: int = PROMOTE_BATCH_SIZE):
    """Parse confirmed raw transactions into tax_lots and tax_disposals.

    Processes rows in batches of ``batch_size`` (default 500), committing
    after each batch. Because each row is flipped to ``status='promoted'``
    as part of its promotion, successive fetches naturally exclude
    already-processed rows — the outer loop terminates as soon as the
    ``WHERE status='confirmed'`` query returns zero rows.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        promoted = 0
        while True:
            cur.execute(
                """
                SELECT id, source, chain, raw_json, timestamp
                FROM tax_raw_transactions
                WHERE status = 'confirmed'
                ORDER BY timestamp ASC, id ASC
                LIMIT %s
                """,
                (batch_size,),
            )
            rows = cur.fetchall()
            if not rows:
                break

            for row in rows:
                raw = row["raw_json"] if isinstance(row["raw_json"], dict) else json.loads(row["raw_json"])
                raw_id = row["id"]
                chain = row["chain"] or raw.get("chain", "")
                ts = row["timestamp"]
                source_tx_id = raw.get("source_tx_id")
                tx_type = raw.get("type", "").lower()
                symbol = raw.get("symbol", "")
                wallet = raw.get("wallet_address")
                # Item 1.1: resolve or create the (source, wallet, chain)
                # account row so new lots and disposals are pinned to a
                # basis pool.  raw["source"] is the ingest origin and may
                # differ from the raw-transaction row's ``source`` column
                # (e.g. CSV rows store "coinbase" in raw_json but live under
                # the ``csv`` source column) — prefer the raw_json value
                # when present.
                account_source = (
                    raw.get("source") or row.get("source") or "unknown"
                )
                account_id = get_or_create_account(
                    conn, account_source, wallet, chain
                )

                quantity = Decimal(str(raw.get("quantity", 0)))
                price_usd = Decimal(str(raw.get("price_usd", 0)))
                fee_usd = Decimal(str(raw.get("fee_usd", 0)))

                if tx_type in BUY_TYPES or tx_type == "swap":
                    # Create a lot for the acquired asset
                    cost_basis = quantity * price_usd + fee_usd
                    cost_per_unit = (cost_basis / quantity) if quantity > 0 else Decimal(0)
                    acq_type = _parse_acquisition_type(raw)
                    # Roadmap 2.4 / Notice 2023-27: NFT collectibles must
                    # land on tax_lots with asset_class='nft_collectible'
                    # so the filing-package collectibles split can route
                    # the eventual disposal onto the 28%-rate Form 8949.
                    # Validated against the column CHECK constraint
                    # (migration 009) — anything else falls back to the
                    # default 'fungible'.
                    asset_class = (raw.get("asset_class") or "fungible").lower()
                    if asset_class not in (
                        "fungible", "nft_collectible", "nft_non_collectible"
                    ):
                        log.warning(
                            "promote_confirmed: unknown asset_class %r — "
                            "defaulting to 'fungible'", asset_class,
                        )
                        asset_class = "fungible"

                    # Dedup check
                    if source_tx_id:
                        cur.execute(
                            "SELECT id FROM tax_lots WHERE source_tx_id = %s",
                            (source_tx_id + ":lot",))
                        if cur.fetchone():
                            # Already promoted — skip
                            _mark_promoted(cur, raw_id)
                            promoted += 1
                            continue

                    cur.execute("""
                        INSERT INTO tax_lots
                            (account_id, wallet_address, chain, symbol,
                             acquired_at, quantity, cost_basis_usd,
                             cost_basis_per_unit, remaining_quantity,
                             acquisition_type, fee_usd, source,
                             source_tx_id, raw_transaction_id, asset_class)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (account_id, wallet, chain, symbol, ts, quantity,
                          cost_basis, cost_per_unit, quantity,
                          acq_type, fee_usd, raw.get("source", "csv"),
                          (source_tx_id + ":lot") if source_tx_id else None,
                          raw_id, asset_class))

                if tx_type in SELL_TYPES or tx_type == "swap":
                    # Create a disposal for the sold/sent asset
                    sell_symbol = raw.get("sell_symbol", symbol) if tx_type == "swap" else symbol
                    sell_qty = Decimal(str(raw.get("sell_quantity", quantity))) if tx_type == "swap" else quantity
                    proceeds = sell_qty * price_usd if tx_type != "swap" else Decimal(str(raw.get("proceeds_usd", 0)))

                    if tx_type in SELL_TYPES:
                        proceeds = quantity * price_usd

                    sell_source_tx_id = (source_tx_id + ":disposal") if source_tx_id else None

                    if sell_source_tx_id:
                        cur.execute(
                            "SELECT id FROM tax_disposals WHERE source_tx_id = %s",
                            (sell_source_tx_id,))
                        if cur.fetchone():
                            _mark_promoted(cur, raw_id)
                            promoted += 1
                            continue

                    cur.execute("""
                        INSERT INTO tax_disposals
                            (account_id, wallet_address, chain, symbol,
                             disposed_at, quantity, proceeds_usd, fee_usd,
                             source, source_tx_id, raw_transaction_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (account_id, wallet, chain, sell_symbol, ts,
                          sell_qty, proceeds, fee_usd,
                          raw.get("source", "csv"),
                          sell_source_tx_id, raw_id))

                _mark_promoted(cur, raw_id)
                promoted += 1

            # Commit after each batch so long-running promotions don't hold
            # an ever-growing transaction.
            conn.commit()

        log.info("Promoted %d transactions", promoted)
        return promoted
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _mark_promoted(cur, raw_id: int):
    """Mark a raw transaction as promoted."""
    cur.execute("""
        UPDATE tax_raw_transactions
        SET status = 'promoted', promoted_at = NOW()
        WHERE id = %s
    """, (raw_id,))


def audit_rebuild(conn, method: str = "fifo") -> dict:
    """Full clean-room rebuild from raw source data.

    Returns the ``rematch_all`` summary dict augmented with the number of
    raw transactions promoted during this rebuild.
    """
    from crowtax_engine.engine import rematch_all

    cur = conn.cursor()
    try:
        # Wipe derived tables (order matters for FK constraints)
        cur.execute("DELETE FROM tax_lot_matches")
        cur.execute("DELETE FROM tax_disposals")
        cur.execute("DELETE FROM tax_lots")

        # Reset promoted back to confirmed
        cur.execute("""
            UPDATE tax_raw_transactions
            SET status = 'confirmed', promoted_at = NULL
            WHERE status = 'promoted'
        """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

    # Re-derive all lots and disposals
    promoted = promote_confirmed(conn)

    # Re-match everything
    summary = rematch_all(conn, method)
    summary["promoted"] = promoted

    log.info("Audit rebuild complete with method=%s: %s", method, summary)
    return summary
