"""Tests for roadmap item 1.2 — wallet-to-wallet transfer handling.

Covers:
    * ``record_transfer`` inserts a row with the expected default status.
    * ``apply_transfer`` clones source lots to destination account with
      preserved ``acquired_at`` and ``cost_basis_per_unit``.
    * Source lot ``remaining_quantity`` decrements; no disposal rows
      appear.
    * Quantity conservation across accounts.
    * ``pair_legs`` joins an outbound leg to a matching inbound leg
      inside the time + quantity tolerances.
    * Unmatched legs stay unmatched (never silently classified).
    * ``apply_transfer`` refuses to run twice for the same transfer.
    * Transfer with a fee rolls the fee into the first destination lot.
    * A downstream disposal at the destination consumes the cloned lot
      (end-to-end integration with item 1.1 matching).
"""

from __future__ import annotations

from decimal import Decimal

import psycopg2.extras
import pytest

from crowtax_engine import engine, transfers
from tests.builders import make_account, make_disposal, make_lot


def _count(conn, table):
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    finally:
        cur.close()


def _get_lot(conn, lot_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM tax_lots WHERE id = %s", (lot_id,))
        return cur.fetchone()
    finally:
        cur.close()


# ----------------------------------------------------------------------
# record_transfer
# ----------------------------------------------------------------------

def test_record_transfer_with_both_accounts_is_matched(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="0.5",
        transferred_at=1_700_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    cur = db.cursor()
    cur.execute("SELECT status FROM tax_transfers WHERE id = %s", (tid,))
    assert cur.fetchone()[0] == "matched"
    cur.close()


def test_record_transfer_missing_leg_is_unmatched(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="0.5",
        transferred_at=1_700_000_000,
        from_account_id=acc_a,
    )
    cur = db.cursor()
    cur.execute("SELECT status FROM tax_transfers WHERE id = %s", (tid,))
    assert cur.fetchone()[0] == "unmatched"
    cur.close()


# ----------------------------------------------------------------------
# apply_transfer: happy path
# ----------------------------------------------------------------------

def test_apply_transfer_clones_lot_preserving_basis_and_acquired_at(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    # One BTC lot at coinbase at $30k basis.
    src_lot = make_lot(
        db, symbol="BTC", quantity=1, price_usd=30000,
        acquired_at="2024-06-01", account_id=acc_a, chain="BTC",
    )

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    new_ids = transfers.apply_transfer(db, tid)
    assert len(new_ids) == 1

    dest = _get_lot(db, new_ids[0])
    src = _get_lot(db, src_lot)

    # Destination lot: same acquired_at, same per-unit basis.
    assert dest["account_id"] == acc_b
    assert dest["acquired_at"] == src["acquired_at"]
    assert Decimal(str(dest["cost_basis_per_unit"])) == Decimal("30000.000000")
    assert Decimal(str(dest["quantity"])) == Decimal("1.000000000000000000")
    assert Decimal(str(dest["remaining_quantity"])) == Decimal("1.000000000000000000")
    assert dest["transfer_id"] == tid
    assert dest["parent_lot_id"] == src_lot

    # Source lot drained to zero.
    assert Decimal(str(src["remaining_quantity"])) == Decimal("0.000000000000000000")

    # No disposal produced.
    assert _count(db, "tax_disposals") == 0

    # Transfer now status='applied' with applied_at set.
    cur = db.cursor()
    cur.execute(
        "SELECT status, applied_at FROM tax_transfers WHERE id = %s", (tid,))
    status, applied_at = cur.fetchone()
    cur.close()
    assert status == "applied"
    assert applied_at is not None


def test_apply_transfer_partial_uses_fifo_at_source(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    lot1 = make_lot(db, symbol="BTC", quantity=1, price_usd=20000,
                    acquired_at="2024-01-01", account_id=acc_a, chain="BTC")
    lot2 = make_lot(db, symbol="BTC", quantity=1, price_usd=40000,
                    acquired_at="2024-06-01", account_id=acc_a, chain="BTC")

    # Transfer 1.5 BTC — consumes lot1 fully + 0.5 of lot2.
    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1.5",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    new_ids = transfers.apply_transfer(db, tid)
    assert len(new_ids) == 2

    src1 = _get_lot(db, lot1)
    src2 = _get_lot(db, lot2)
    assert Decimal(str(src1["remaining_quantity"])) == Decimal(0)
    assert Decimal(str(src2["remaining_quantity"])) == Decimal("0.500000000000000000")

    # Cloned lots carry their parent's basis.
    d1 = _get_lot(db, new_ids[0])
    d2 = _get_lot(db, new_ids[1])
    assert d1["parent_lot_id"] == lot1
    assert d2["parent_lot_id"] == lot2
    assert Decimal(str(d1["cost_basis_per_unit"])) == Decimal("20000.000000")
    assert Decimal(str(d2["cost_basis_per_unit"])) == Decimal("40000.000000")


def test_apply_transfer_quantity_is_conserved_across_accounts(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    make_lot(db, symbol="BTC", quantity=2, price_usd=25000,
             acquired_at="2024-01-01", account_id=acc_a, chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="0.5",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    transfers.apply_transfer(db, tid)

    cur = db.cursor()
    cur.execute(
        """
        SELECT account_id, COALESCE(SUM(remaining_quantity), 0)
        FROM tax_lots WHERE symbol = 'BTC'
        GROUP BY account_id ORDER BY account_id
        """
    )
    rows = dict(cur.fetchall())
    cur.close()
    assert Decimal(str(rows[acc_a])) == Decimal("1.500000000000000000")
    assert Decimal(str(rows[acc_b])) == Decimal("0.500000000000000000")
    # Grand total unchanged.
    assert Decimal(str(rows[acc_a])) + Decimal(str(rows[acc_b])) == Decimal("2.000000000000000000")


# ----------------------------------------------------------------------
# apply_transfer: guardrails
# ----------------------------------------------------------------------

def test_apply_transfer_refuses_double_application(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc_a, chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    transfers.apply_transfer(db, tid)
    with pytest.raises(ValueError, match="already applied"):
        transfers.apply_transfer(db, tid)


def test_apply_transfer_refuses_missing_leg(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc_a, chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a,  # no to_account_id
    )
    with pytest.raises(ValueError, match="missing account legs"):
        transfers.apply_transfer(db, tid)


def test_apply_transfer_refuses_insufficient_source_basis(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")
    make_lot(db, symbol="BTC", quantity="0.3", price_usd=30000,
             acquired_at="2024-01-01", account_id=acc_a, chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    with pytest.raises(ValueError, match="insufficient basis"):
        transfers.apply_transfer(db, tid)


# ----------------------------------------------------------------------
# Fee placement
# ----------------------------------------------------------------------

def test_transfer_fee_rolls_into_first_destination_lot_basis(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="ETH")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="ETH")
    make_lot(db, symbol="ETH", quantity=2, price_usd=2000,
             acquired_at="2024-01-01", account_id=acc_a, chain="ETH")

    tid = transfers.record_transfer(
        db, symbol="ETH", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
        fee_usd="10",
    )
    new_ids = transfers.apply_transfer(db, tid)
    dest = _get_lot(db, new_ids[0])
    # Basis = 1 * 2000 + 10 fee = 2010.
    assert Decimal(str(dest["cost_basis_usd"])) == Decimal("2010.000000")
    assert Decimal(str(dest["cost_basis_per_unit"])) == Decimal("2010.000000")
    assert Decimal(str(dest["fee_usd"])) == Decimal("10.000000")


# ----------------------------------------------------------------------
# pair_legs
# ----------------------------------------------------------------------

def test_pair_legs_joins_outbound_to_inbound_within_window(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    t_out = 1_750_000_000
    t_in = t_out + 3600  # 1h later
    out_id = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=t_out, from_account_id=acc_a,
    )
    in_id = transfers.record_transfer(
        db, symbol="BTC", quantity="0.999",  # 0.1% fee
        transferred_at=t_in, to_account_id=acc_b,
    )

    pairs = transfers.pair_legs(db)
    assert pairs == 1

    cur = db.cursor()
    cur.execute(
        "SELECT status, from_account_id, to_account_id FROM tax_transfers WHERE id = %s",
        (out_id,),
    )
    status, fid, tid = cur.fetchone()
    assert status == "matched"
    assert fid == acc_a and tid == acc_b

    cur.execute("SELECT status FROM tax_transfers WHERE id = %s", (in_id,))
    assert cur.fetchone()[0] == "merged"
    cur.close()


def test_pair_legs_rejects_quantity_outside_tolerance(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000, from_account_id=acc_a,
    )
    transfers.record_transfer(
        db, symbol="BTC", quantity="0.5",  # 50% delta > default 2%
        transferred_at=1_750_000_000 + 3600, to_account_id=acc_b,
    )
    pairs = transfers.pair_legs(db)
    assert pairs == 0
    unmatched = transfers.list_unmatched(db)
    assert len(unmatched) == 2


def test_pair_legs_rejects_outside_time_window(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000, from_account_id=acc_a,
    )
    transfers.record_transfer(
        db, symbol="BTC", quantity="0.999",
        transferred_at=1_750_000_000 + 48 * 3600,  # 48h later
        to_account_id=acc_b,
    )
    pairs = transfers.pair_legs(db)
    assert pairs == 0


def test_pair_legs_leaves_unmatched_leg_alone(db):
    """A solo outbound with no inbound stays status='unmatched'."""
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")

    transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000, from_account_id=acc_a,
    )
    pairs = transfers.pair_legs(db)
    assert pairs == 0
    unmatched = transfers.list_unmatched(db)
    assert len(unmatched) == 1
    assert unmatched[0]["from_account_id"] == acc_a
    assert unmatched[0]["to_account_id"] is None


# ----------------------------------------------------------------------
# End-to-end: transfer + downstream disposal
# ----------------------------------------------------------------------

def test_disposal_at_destination_consumes_cloned_lot(db):
    """Round trip: transfer applied, then sell at destination — the
    gain is measured against the original basis (no phantom gain)."""
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    # Bought at coinbase for $30k.
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-06-01", account_id=acc_a, chain="BTC")

    # Transferred to self-custody (no disposal).
    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    transfers.apply_transfer(db, tid)

    # Sold at self-custody for $50k later.
    dsp = make_disposal(
        db, symbol="BTC", quantity="1", proceeds_usd=50000,
        disposed_at="2025-06-01", account_id=acc_b, chain="BTC",
    )
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    # Gain is $20k (proceeds $50k - basis $30k), NOT $50k.
    assert Decimal(str(matches[0].gain_loss_usd)) == Decimal("20000.000000")
    # Long-term: acquired 2024-06-01, disposed 2025-06-01 — exactly one
    # year, so still short. This tests holding-period tacking.
    assert matches[0].holding_period == "short"


def test_transfer_preserves_long_term_holding_period(db):
    """Transferred coins keep their original acquired_at — so a >1yr
    hold remains long-term even if the destination lot is brand new."""
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="self", wallet_address="0xcafe", chain="BTC")

    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2023-01-01", account_id=acc_a, chain="BTC")

    tid = transfers.record_transfer(
        db, symbol="BTC", quantity="1",
        transferred_at=1_750_000_000,
        from_account_id=acc_a, to_account_id=acc_b,
    )
    transfers.apply_transfer(db, tid)

    dsp = make_disposal(
        db, symbol="BTC", quantity="1", proceeds_usd=50000,
        disposed_at="2025-06-01", account_id=acc_b, chain="BTC",
    )
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert matches[0].holding_period == "long"
