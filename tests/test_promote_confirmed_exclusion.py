"""Tests for promote_confirmed(exclusions=...) — internal-transfer skip-list.

Engine consumers that don't go through CrowTax's translate-layer filter
can pass an ExclusionSet directly to promote_confirmed; rows whose
``raw_json["source_tx_id"]`` appears in the set get advanced to
``status='promoted'`` without producing any tax_lots or tax_disposals
(they're one half of a non-taxable internal transfer).
"""

from __future__ import annotations

from crowtax_engine import staging
from crowtax_engine.transfer_pairs import ExclusionSet


def _ingest_buy(db, source_tx_id: str, qty: float = 1.0, price: float = 100.0):
    return staging.ingest_raw(
        db,
        source="executor",
        chain="BTC",
        timestamp=1735689600,  # 2025-01-01
        raw_json={
            "type": "buy",
            "symbol": "BTC",
            "quantity": qty,
            "price_usd": price,
            "fee_usd": 0,
            "source": "coinbase",
            "wallet_address": "test-wallet",
        },
        source_tx_id=source_tx_id,
        status="confirmed",
    )


def _count_lots(db) -> int:
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM tax_lots")
    n = cur.fetchone()[0]
    cur.close()
    return int(n)


def _row_status(db, raw_id: int) -> str:
    cur = db.cursor()
    cur.execute(
        "SELECT status FROM tax_raw_transactions WHERE id = %s", (raw_id,)
    )
    s = cur.fetchone()[0]
    cur.close()
    return str(s)


def test_no_exclusions_promotes_normally(db) -> None:
    raw_id = _ingest_buy(db, "tx-keep-1")
    staging.promote_confirmed(db)
    assert _count_lots(db) == 1
    assert _row_status(db, raw_id) == "promoted"


def test_excluded_source_tx_id_skipped(db) -> None:
    """The buy with source_tx_id 'tx-skip' is in the exclusion set; no
    tax_lot is created, but the row still flips to 'promoted' so the
    pipeline doesn't loop on it forever."""
    keep_id = _ingest_buy(db, "tx-keep-2")
    skip_id = _ingest_buy(db, "tx-skip")

    excl = ExclusionSet(
        out_event_ids=frozenset({"tx-skip"}),
        in_event_ids=frozenset(),
    )
    staging.promote_confirmed(db, exclusions=excl)

    # Only the non-skipped row produced a lot.
    assert _count_lots(db) == 1
    # Both raw rows advance to 'promoted' (otherwise the loop never ends).
    assert _row_status(db, keep_id) == "promoted"
    assert _row_status(db, skip_id) == "promoted"


def test_exclusion_in_either_leg_skips(db) -> None:
    """Membership predicate covers both out_event_ids and in_event_ids."""
    out_id = _ingest_buy(db, "tx-out")
    in_id = _ingest_buy(db, "tx-in")

    excl = ExclusionSet(
        out_event_ids=frozenset({"tx-out"}),
        in_event_ids=frozenset({"tx-in"}),
    )
    staging.promote_confirmed(db, exclusions=excl)

    assert _count_lots(db) == 0
    assert _row_status(db, out_id) == "promoted"
    assert _row_status(db, in_id) == "promoted"
