"""Tests for roadmap item 1.1 — per-wallet / per-account basis tracking.

Covers:
    * ``tax_accounts`` resolution is idempotent for the same triple.
    * ``match_disposal`` only consumes lots from the disposal's account.
    * A disposal at account B does not drain basis out of account A.
    * Legacy (``account_id IS NULL``) rows still match against the
      universal pool.
    * Remaining-quantity conservation across accounts equals the
      sum of lot quantities minus matched quantities.
    * ``rematch_all`` produces identical numbers when every row is
      correctly pinned to its account.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg2.extras

from crowtax_engine import accounts, engine
from tests.builders import make_account, make_disposal, make_lot

# ----------------------------------------------------------------------
# Account resolution
# ----------------------------------------------------------------------

def test_get_or_create_account_is_idempotent(db):
    a = accounts.get_or_create_account(db, "coinbase", "user-abc", "BTC")
    b = accounts.get_or_create_account(db, "coinbase", "user-abc", "BTC")
    assert a == b

    listed = accounts.list_accounts(db)
    assert len(listed) == 1
    assert listed[0]["source"] == "coinbase"
    assert listed[0]["wallet_address"] == "user-abc"
    assert listed[0]["chain"] == "BTC"


def test_account_canonicalization_lowercases_hex(db):
    a = accounts.get_or_create_account(
        db, "onchain", "0xABCDEFabcdef0123456789ABCDEF0123456789AB", "ETH"
    )
    b = accounts.get_or_create_account(
        db, "onchain", "0xabcdefABCDEF0123456789abcdef0123456789ab", "ETH"
    )
    assert a == b


def test_account_empty_wallet_defaults_to_placeholder(db):
    aid = accounts.get_or_create_account(db, "executor", "", "HYPE")
    row = accounts.get_account(db, aid)
    assert row["wallet_address"] == "executor:default"


# ----------------------------------------------------------------------
# Per-wallet matching
# ----------------------------------------------------------------------

def _count_matches(conn, disposal_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM tax_lot_matches WHERE disposal_id = %s",
            (disposal_id,),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _sum_matched_basis(conn, disposal_id):
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT COALESCE(SUM(cost_basis_usd), 0)
            FROM tax_lot_matches WHERE disposal_id = %s
            """,
            (disposal_id,),
        )
        return Decimal(str(cur.fetchone()[0]))
    finally:
        cur.close()


def test_disposal_at_account_b_does_not_consume_account_a_basis(db):
    """Two accounts each hold 1 BTC; a sale at B only eats B's basis."""
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="kraken", wallet_address="B", chain="BTC")

    lot_a = make_lot(
        db, symbol="BTC", quantity=1, price_usd=30000,
        acquired_at="2024-06-01", account_id=acc_a, chain="BTC",
    )
    lot_b = make_lot(
        db, symbol="BTC", quantity=1, price_usd=50000,
        acquired_at="2024-10-01", account_id=acc_b, chain="BTC",
    )

    # Sell 0.5 BTC at account B.
    dsp = make_disposal(
        db, symbol="BTC", quantity="0.5", proceeds_usd=35000,
        disposed_at="2025-02-01", account_id=acc_b, chain="BTC",
    )

    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    assert matches[0].lot_id == lot_b, (
        f"Expected sale at account B to consume lot B ({lot_b}), "
        f"but it consumed lot {matches[0].lot_id}"
    )
    # Basis should be 0.5 * 50000 = 25000.
    assert Decimal(str(matches[0].cost_basis_usd)) == Decimal("25000")

    # Lot A is untouched.
    cur = db.cursor()
    cur.execute("SELECT remaining_quantity FROM tax_lots WHERE id = %s",
                (lot_a,))
    assert Decimal(str(cur.fetchone()[0])) == Decimal("1.000000000000000000")
    cur.close()


def test_per_wallet_rematch_conserves_quantity_across_accounts(db):
    """Sum of remaining + matched across accounts equals sum of acquired."""
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="kraken", wallet_address="B", chain="BTC")

    make_lot(db, symbol="BTC", quantity=2, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc_a, chain="BTC")
    make_lot(db, symbol="BTC", quantity=3, price_usd=40000,
             acquired_at="2024-02-01", account_id=acc_b, chain="BTC")

    make_disposal(db, symbol="BTC", quantity="0.7", proceeds_usd=35000,
                  disposed_at="2025-03-01", account_id=acc_a, chain="BTC")
    make_disposal(db, symbol="BTC", quantity="1.5", proceeds_usd=80000,
                  disposed_at="2025-03-02", account_id=acc_b, chain="BTC")

    summary = engine.rematch_all(db, method="fifo")
    assert summary["unmatched_count"] == 0

    cur = db.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(remaining_quantity), 0)
        FROM tax_lots
        """
    )
    q_total, q_remaining = cur.fetchone()
    cur.execute("SELECT COALESCE(SUM(quantity_matched), 0) FROM tax_lot_matches")
    q_matched = cur.fetchone()[0]
    cur.close()

    # qty conservation: sum(acquired) == sum(remaining) + sum(matched)
    assert (Decimal(str(q_total))
            == Decimal(str(q_remaining)) + Decimal(str(q_matched)))


def test_legacy_rows_without_account_id_use_universal_pool(db):
    """account_id=NULL lots still match against account_id=NULL disposals."""
    # Two "legacy" lots and one legacy disposal — no account_id on any.
    make_lot(db, symbol="ETH", quantity=2, price_usd=1500,
             acquired_at="2023-05-01", account_id=None, chain="ETH")
    make_lot(db, symbol="ETH", quantity=1, price_usd=2000,
             acquired_at="2023-06-01", account_id=None, chain="ETH")
    dsp = make_disposal(db, symbol="ETH", quantity="1", proceeds_usd=2500,
                        disposed_at="2024-01-01", account_id=None, chain="ETH")

    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    # FIFO -> first (May) lot is consumed first.
    assert Decimal(str(matches[0].cost_basis_usd)) == Decimal("1500")


def test_per_account_fifo_ordering(db):
    """Within an account, FIFO uses acquired_at ascending."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=20000,
             acquired_at="2024-02-01", account_id=acc, chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=40000,
             acquired_at="2024-03-01", account_id=acc, chain="BTC")

    # Sell 2 BTC — consumes Jan lot then Feb lot.
    dsp = make_disposal(db, symbol="BTC", quantity="2", proceeds_usd=60000,
                        disposed_at="2025-05-01", account_id=acc, chain="BTC")
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 2
    total_basis = sum(Decimal(str(m.cost_basis_usd)) for m in matches)
    assert total_basis == Decimal("50000")  # 30000 + 20000


def test_specific_id_refuses_lots_from_different_account(db):
    """SpecID path must not accept a lot id from a different account."""
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="kraken", wallet_address="B", chain="BTC")
    lot_a = make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
                     acquired_at="2024-01-01", account_id=acc_a, chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=40000,
             acquired_at="2024-02-01", account_id=acc_b, chain="BTC")

    dsp = make_disposal(db, symbol="BTC", quantity="0.5", proceeds_usd=20000,
                        disposed_at="2025-05-01", account_id=acc_b, chain="BTC")

    # Caller tries to force account A's lot onto account B's disposal.
    matches = engine.match_disposal(
        db, dsp, method="specific_id", specific_lot_ids=[lot_a],
    )
    # Engine rejects the mismatched lot; no matches produced.
    assert matches == [], (
        "SpecID accepted a lot from a different account — violates "
        "per-wallet basis (item 1.1)."
    )


def test_specific_id_refuses_lots_of_different_symbol(db):
    """SpecID path must not accept a lot id of a different symbol."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="none")
    lot_eth = make_lot(db, symbol="ETH", quantity=10, price_usd=1800,
                       acquired_at="2024-01-01", account_id=acc, chain="none")

    dsp = make_disposal(db, symbol="BTC", quantity="0.1", proceeds_usd=6000,
                        disposed_at="2025-05-01", account_id=acc, chain="none")

    matches = engine.match_disposal(
        db, dsp, method="specific_id", specific_lot_ids=[lot_eth],
    )
    assert matches == []



# ----------------------------------------------------------------------
# End-to-end: staging pipeline populates account_id
# ----------------------------------------------------------------------

def test_promote_confirmed_populates_account_id(db):
    """A CSV-style ingest promote pins the new lot / disposal to an
    account row resolved from (source, wallet_address, chain)."""
    from crowtax_engine import staging

    # Ingest a buy row.
    staging.ingest_raw(
        db, source="csv", chain="ETH", timestamp=1_700_000_000,
        raw_json={
            "source": "coinbase",
            "source_tx_id": "cb-buy-1",
            "type": "buy",
            "symbol": "ETH",
            "quantity": "1",
            "price_usd": "1800",
            "fee_usd": "5",
            "wallet_address": "user-abc",
            "chain": "ETH",
        },
        source_tx_id="cb-buy-1",
        status="confirmed",
    )
    # And a later sell row at the same account.
    staging.ingest_raw(
        db, source="csv", chain="ETH", timestamp=1_750_000_000,
        raw_json={
            "source": "coinbase",
            "source_tx_id": "cb-sell-1",
            "type": "sell",
            "symbol": "ETH",
            "quantity": "0.5",
            "price_usd": "2400",
            "fee_usd": "3",
            "wallet_address": "user-abc",
            "chain": "ETH",
        },
        source_tx_id="cb-sell-1",
        status="confirmed",
    )

    staging.promote_confirmed(db)

    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, account_id FROM tax_lots")
    lot_rows = cur.fetchall()
    cur.execute("SELECT id, account_id FROM tax_disposals")
    dsp_rows = cur.fetchall()
    cur.close()

    assert len(lot_rows) == 1
    assert len(dsp_rows) == 1
    assert lot_rows[0]["account_id"] is not None
    assert dsp_rows[0]["account_id"] is not None
    assert lot_rows[0]["account_id"] == dsp_rows[0]["account_id"], (
        "Coinbase buy and sell at the same wallet should resolve to the "
        "same account row."
    )
