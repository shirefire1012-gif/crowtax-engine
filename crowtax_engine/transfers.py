"""Wallet-to-wallet transfer handling.

Roadmap item 1.2.  Under per-wallet basis (item 1.1), transfers between
accounts owned by the same taxpayer are non-dispositive: basis and
``acquired_at`` ride with the coins.  Without this module, every
withdraw-deposit pair across two of our own accounts would look like a
disposal at the source followed by a fresh FMV-basis lot at the
destination - producing phantom gains and resetting holding periods.

Authorities:
    * IRC section 1223(2) - tacked holding period on carryover-basis
      transfers.
    * Rev. Rul. 2019-24 / Notice 2014-21 - transfers between the
      taxpayer's own wallets are not dispositions.
    * Rev. Proc. 2024-28 - per-wallet basis mandate (the reason this
      module is load-bearing rather than nice-to-have).

Design:

``tax_transfers`` rows are the single source of truth for non-dispositive
movements.  A row is created in status ``'unmatched'``; the matcher
pairs inbound + outbound legs and flips to ``'matched'`` once both
account FKs are populated, then ``'applied'`` once the destination lots
are materialised.  Unmatched rows are never silently classified as
disposals - they stay visible in the ledger for manual review (CPA may
classify as gift, payment, or loss of control).

``apply_transfer`` clones each source lot to the destination account
with preserved ``acquired_at`` and ``cost_basis_per_unit``, decrements
the source lot ``remaining_quantity``, and creates **no** disposal row.
The new lots carry ``parent_lot_id`` + ``transfer_id`` pointers so the
audit trail reconstructs the transfer chain.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import psycopg2.extras

log = logging.getLogger(__name__)

# Default time-window tolerance for pairing a withdraw leg to a deposit
# leg on the same symbol.  Covers exchange settlement delays and
# on-chain confirmation latency.
DEFAULT_MATCH_WINDOW_SECONDS = 24 * 60 * 60

# Quantity tolerance for pairing legs.  Networks charge withdrawal fees
# so the deposit will typically be slightly smaller than the withdraw.
# We accept a small relative mismatch; larger drift requires manual
# review.
DEFAULT_QUANTITY_TOLERANCE = Decimal("0.02")  # 2%


def record_transfer(
    conn,
    *,
    symbol: str,
    quantity,
    transferred_at: int,
    from_account_id: Optional[int] = None,
    to_account_id: Optional[int] = None,
    fee_usd=0,
    raw_transaction_id: Optional[int] = None,
    paired_raw_transaction_id: Optional[int] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None,
) -> int:
    """Insert a ``tax_transfers`` row and return its id.

    Status defaults:
        * ``'matched'`` when both ``from_account_id`` and
          ``to_account_id`` are set (ready for ``apply_transfer``).
        * ``'unmatched'`` otherwise - requires manual resolution or a
          later call to ``pair_legs``.
    """
    quantity = Decimal(str(quantity))
    fee_usd = Decimal(str(fee_usd))

    if status is None:
        status = "matched" if (from_account_id and to_account_id) else "unmatched"

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_transfers
                (from_account_id, to_account_id, symbol, quantity,
                 transferred_at, fee_usd, status, notes,
                 raw_transaction_id, paired_raw_transaction_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (from_account_id, to_account_id, symbol, quantity,
             transferred_at, fee_usd, status, notes,
             raw_transaction_id, paired_raw_transaction_id),
        )
        tid = cur.fetchone()[0]
        conn.commit()
        return tid
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def pair_legs(
    conn,
    *,
    window_seconds: int = DEFAULT_MATCH_WINDOW_SECONDS,
    quantity_tolerance: Decimal = DEFAULT_QUANTITY_TOLERANCE,
) -> int:
    """Pair unmatched outbound legs with unmatched inbound legs.

    An "outbound leg" is a ``tax_transfers`` row with ``from_account_id``
    set but ``to_account_id`` NULL.  "Inbound" is the reverse.  Pairing
    rules:

        * Same symbol.
        * Outbound ``transferred_at`` within ``window_seconds`` of
          inbound ``transferred_at`` (either order within the window).
        * Inbound quantity within ``(1 - tolerance) * outbound`` and
          ``outbound`` (network fees mean inbound is typically smaller
          than or equal to outbound; matches where inbound > outbound
          are rejected because that pattern rarely explains as a fee).

    Pairs are consumed greedily by minimum time delta; leftover legs
    remain unmatched for manual review.  Returns the number of pairs
    created.

    Implementation folds both legs into a single row: the surviving
    outbound row gets ``to_account_id`` populated and ``status='matched'``;
    the inbound leg gets ``status='merged'`` with
    ``paired_raw_transaction_id`` pointing at the surviving row.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, symbol, quantity, transferred_at, from_account_id,
                   fee_usd, raw_transaction_id
            FROM tax_transfers
            WHERE status = 'unmatched'
              AND from_account_id IS NOT NULL
              AND to_account_id IS NULL
            ORDER BY symbol, transferred_at ASC, id ASC
            """
        )
        outbound = cur.fetchall()

        cur.execute(
            """
            SELECT id, symbol, quantity, transferred_at, to_account_id,
                   raw_transaction_id
            FROM tax_transfers
            WHERE status = 'unmatched'
              AND to_account_id IS NOT NULL
              AND from_account_id IS NULL
            ORDER BY symbol, transferred_at ASC, id ASC
            """
        )
        inbound_all = cur.fetchall()

        inbound_by_symbol: dict[str, list[dict]] = {}
        for row in inbound_all:
            inbound_by_symbol.setdefault(row["symbol"], []).append(dict(row))

        pairs = 0
        for out in outbound:
            symbol = out["symbol"]
            out_qty = Decimal(str(out["quantity"]))
            out_ts = out["transferred_at"]
            candidates = inbound_by_symbol.get(symbol, [])

            best_idx = -1
            best_delta = None
            for idx, inn in enumerate(candidates):
                if inn.get("_consumed"):
                    continue
                in_ts = inn["transferred_at"]
                if abs(in_ts - out_ts) > window_seconds:
                    continue
                in_qty = Decimal(str(inn["quantity"]))
                if in_qty > out_qty:
                    continue
                ratio = (out_qty - in_qty) / out_qty if out_qty > 0 else Decimal(0)
                if ratio > quantity_tolerance:
                    continue
                delta = abs(in_ts - out_ts)
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_idx = idx

            if best_idx < 0:
                continue

            inn = candidates[best_idx]
            inn["_consumed"] = True

            # The ``paired_raw_transaction_id`` FK points at
            # ``tax_raw_transactions`` — we only set it when the inbound
            # leg carried a real raw-transaction id.  The cross-reference
            # between the two ``tax_transfers`` rows lives in ``notes``
            # and in the inbound row's ``raw_transaction_id`` lookup.
            inbound_raw = inn.get("raw_transaction_id")
            cur.execute(
                """
                UPDATE tax_transfers
                SET to_account_id = %s,
                    status = 'matched',
                    transferred_at = %s,
                    paired_raw_transaction_id = COALESCE(
                        paired_raw_transaction_id, %s
                    ),
                    notes = COALESCE(notes, '') || %s
                WHERE id = %s
                """,
                (inn["to_account_id"], inn["transferred_at"],
                 inbound_raw,
                 f" [paired with transfer id={inn['id']}]",
                 out["id"]),
            )
            cur.execute(
                """
                UPDATE tax_transfers
                SET status = 'merged',
                    notes = COALESCE(notes, '') || %s
                WHERE id = %s
                """,
                (f" [merged into transfer id={out['id']}]", inn["id"]),
            )
            pairs += 1

        conn.commit()
        log.info("pair_legs: matched %d transfer pairs", pairs)
        return pairs
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def apply_transfer(conn, transfer_id: int) -> list[int]:
    """Apply a matched transfer: clone source lots to destination account.

    Returns the list of newly-created destination ``tax_lots.id`` values.

    Consumes source lots in FIFO (by ``acquired_at``) up to the transfer
    quantity.  Each destination lot preserves:

        * ``acquired_at``        (IRC 1223(2) tacked holding period)
        * ``cost_basis_per_unit`` (non-dispositive; basis rides along)
        * ``acquisition_type``   (original label preserved)

    The transfer's ``fee_usd`` is added to the **first** destination
    lot's basis - this mirrors the position that withdrawal fees
    increase the basis of the coins at the receiving wallet.  Alternative
    treatments (deduct as fee, subtract from source basis) should be
    documented in DECISIONS.md; this is the chosen default.

    Raises if the transfer is already applied, is merged, or lacks
    either account FK.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT * FROM tax_transfers WHERE id = %s FOR UPDATE",
            (transfer_id,),
        )
        xfer = cur.fetchone()
        if xfer is None:
            raise ValueError(f"Transfer {transfer_id} not found")
        if xfer["status"] == "applied":
            raise ValueError(
                f"Transfer {transfer_id} already applied - refusing to "
                "double-promote (would create duplicate destination lots)"
            )
        if xfer["status"] == "merged":
            raise ValueError(
                f"Transfer {transfer_id} is a merged leg; apply the "
                "surviving row instead"
            )
        if xfer["from_account_id"] is None or xfer["to_account_id"] is None:
            raise ValueError(
                f"Transfer {transfer_id} missing account legs "
                f"(from={xfer['from_account_id']}, "
                f"to={xfer['to_account_id']}) - pair first"
            )

        symbol = xfer["symbol"]
        total_qty = Decimal(str(xfer["quantity"]))
        fee_usd = Decimal(str(xfer["fee_usd"] or 0))
        src_account = xfer["from_account_id"]
        dst_account = xfer["to_account_id"]

        cur.execute(
            """
            SELECT id, acquired_at, quantity, cost_basis_per_unit,
                   remaining_quantity, acquisition_type, chain, fee_usd,
                   source
            FROM tax_lots
            WHERE account_id = %s
              AND symbol = %s
              AND remaining_quantity > 0
            ORDER BY acquired_at ASC, id ASC
            FOR UPDATE
            """,
            (src_account, symbol),
        )
        src_lots = cur.fetchall()

        available = sum(Decimal(str(l["remaining_quantity"])) for l in src_lots)
        if available < total_qty:
            raise ValueError(
                f"Transfer {transfer_id}: insufficient basis at source "
                f"account {src_account} ({available} {symbol} available, "
                f"{total_qty} requested)"
            )

        remaining = total_qty
        new_lot_ids: list[int] = []
        fee_applied = False

        cur.execute(
            "SELECT wallet_address, chain FROM tax_accounts WHERE id = %s",
            (dst_account,),
        )
        dst_acct = cur.fetchone()
        dst_wallet = dst_acct["wallet_address"] if dst_acct else None
        dst_chain = dst_acct["chain"] if dst_acct else None

        for lot in src_lots:
            if remaining <= 0:
                break
            lot_remaining = Decimal(str(lot["remaining_quantity"]))
            take = min(remaining, lot_remaining)
            basis_per_unit = Decimal(str(lot["cost_basis_per_unit"]))
            clone_basis = take * basis_per_unit
            extra_fee = Decimal(0)

            if not fee_applied and fee_usd > 0:
                clone_basis = clone_basis + fee_usd
                extra_fee = fee_usd
                fee_applied = True

            clone_per_unit = (clone_basis / take) if take > 0 else basis_per_unit

            cur.execute(
                """
                INSERT INTO tax_lots
                    (account_id, wallet_address, chain, symbol,
                     acquired_at, quantity, cost_basis_usd,
                     cost_basis_per_unit, remaining_quantity,
                     acquisition_type, fee_usd, source,
                     transfer_id, parent_lot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (dst_account, dst_wallet, dst_chain or lot["chain"], symbol,
                 lot["acquired_at"], take, clone_basis, clone_per_unit,
                 take, lot["acquisition_type"], extra_fee,
                 lot["source"], transfer_id, lot["id"]),
            )
            new_lot_ids.append(cur.fetchone()["id"])

            cur.execute(
                """
                UPDATE tax_lots
                SET remaining_quantity = remaining_quantity - %s
                WHERE id = %s
                """,
                (take, lot["id"]),
            )
            remaining -= take

        cur.execute(
            """
            UPDATE tax_transfers
            SET status = 'applied',
                applied_at = NOW()
            WHERE id = %s
            """,
            (transfer_id,),
        )
        conn.commit()
        log.info(
            "Transfer %d applied: %s %s from account %s -> %s; "
            "created %d destination lots",
            transfer_id, total_qty, symbol, src_account, dst_account,
            len(new_lot_ids),
        )
        return new_lot_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def apply_matched(conn) -> int:
    """Apply every transfer currently in status='matched'.

    Returns the number of transfers applied.  Used by the promote
    pipeline so after ``pair_legs`` runs, all paired transfers are
    materialised before ``rematch_all`` runs.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id FROM tax_transfers
            WHERE status = 'matched'
              AND from_account_id IS NOT NULL
              AND to_account_id IS NOT NULL
            ORDER BY transferred_at ASC, id ASC
            """
        )
        ids = [r[0] for r in cur.fetchall()]
    finally:
        cur.close()

    count = 0
    for tid in ids:
        try:
            apply_transfer(conn, tid)
            count += 1
        except Exception as exc:
            log.error("apply_transfer(%d) failed: %s", tid, exc)
    return count


def list_unmatched(conn) -> list[dict]:
    """Return the unmatched transfer queue for manual-review tooling."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT * FROM tax_transfers
            WHERE status = 'unmatched'
            ORDER BY symbol, transferred_at ASC, id ASC
            """
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
