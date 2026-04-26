"""Tests for roadmap item 1.3 - fee placement correctness.

Buy-side fees increase basis; sell-side fees reduce proceeds. IRC
section 1001(b), Treas. Reg. section 1.1001-1, IRC section 1012, and
Commissioner v. Woodward, 397 U.S. 572 (1970).

Covered:
    * Buy-side fee inflates a lot's cost_basis_usd / cost_basis_per_unit
      via the staging pipeline (already set in staging.promote_confirmed;
      regress here).
    * Sell-side fee reduces effective proceeds in match_disposal so the
      recorded proceeds_usd and gain_loss_usd are fee-net.
    * Property-style: shifting sell fee by $X shifts gain by exactly -$X
      across FIFO/LIFO/HIFO.
    * Invariant: gross_proceeds - sell_fee - sum(match.cost_basis) ==
      sum(match.gain_loss) within $0.01.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crowtax_engine import engine
from tests.builders import make_account, make_disposal, make_lot


def _sum_match(conn, disposal_id, column):
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT COALESCE(SUM({column}), 0) FROM tax_lot_matches "
            f"WHERE disposal_id = %s",
            (disposal_id,),
        )
        return Decimal(str(cur.fetchone()[0]))
    finally:
        cur.close()


def test_sell_fee_reduces_recorded_proceeds(db):
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")

    # Gross $50k, $100 fee -> net $49,900 proceeds.
    dsp = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=50000, fee_usd=100,
        disposed_at="2025-05-01", account_id=acc, chain="BTC",
    )
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    assert Decimal(str(matches[0].proceeds_usd)) == Decimal("49900.000000")
    # Gain = 49,900 - 30,000 = 19,900 (not 20,000).
    assert Decimal(str(matches[0].gain_loss_usd)) == Decimal("19900.000000")


def test_buy_fee_is_included_in_lot_basis(db):
    """Buy-side fee flows into cost_basis_usd at lot creation."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    lot_id = make_lot(
        db, symbol="BTC", quantity=1, price_usd=30000, fee_usd=50,
        acquired_at="2024-01-01", account_id=acc, chain="BTC",
    )
    cur = db.cursor()
    cur.execute(
        "SELECT cost_basis_usd, cost_basis_per_unit, fee_usd FROM tax_lots "
        "WHERE id = %s", (lot_id,))
    basis, per_unit, fee = cur.fetchone()
    cur.close()
    assert Decimal(str(basis)) == Decimal("30050.000000")
    assert Decimal(str(per_unit)) == Decimal("30050.000000")
    assert Decimal(str(fee)) == Decimal("50.000000")


def test_fee_invariant_proceeds_minus_basis_equals_gain(db):
    """For every disposal: gross - fee - sum(basis) == sum(gain)."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="ETH")
    make_lot(db, symbol="ETH", quantity=3, price_usd=1500, fee_usd=30,
             acquired_at="2024-01-01", account_id=acc, chain="ETH")

    gross = Decimal("9000")
    sell_fee = Decimal("25")
    dsp = make_disposal(
        db, symbol="ETH", quantity=3, proceeds_usd=gross, fee_usd=sell_fee,
        disposed_at="2025-05-01", account_id=acc, chain="ETH",
    )
    engine.match_disposal(db, dsp, method="fifo")

    basis_total = _sum_match(db, dsp, "cost_basis_usd")
    gain_total = _sum_match(db, dsp, "gain_loss_usd")
    proceeds_total = _sum_match(db, dsp, "proceeds_usd")

    # Invariant 1: recorded proceeds equal gross - fee.
    assert proceeds_total == gross - sell_fee
    # Invariant 2: net proceeds minus basis equals gain.
    assert (gross - sell_fee) - basis_total == gain_total


@pytest.mark.parametrize("method", ["fifo", "lifo", "hifo"])
def test_sell_fee_shift_equals_gain_shift_for_all_methods(db, method):
    """Shifting sell fee by $X shifts gain by exactly -$X."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=20000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=40000,
             acquired_at="2024-06-01", account_id=acc, chain="BTC")

    # Run once with fee=0.
    dsp0 = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=50000, fee_usd=0,
        disposed_at="2025-05-01", account_id=acc, chain="BTC",
    )
    engine.match_disposal(db, dsp0, method=method)
    gain0 = _sum_match(db, dsp0, "gain_loss_usd")

    # Run again with fee=200 (same method, same lot targets).
    # Reset remaining_quantities and matches.
    cur = db.cursor()
    cur.execute("DELETE FROM tax_lot_matches")
    cur.execute("UPDATE tax_lots SET remaining_quantity = quantity")
    db.commit()
    cur.close()

    dsp1 = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=50000, fee_usd=200,
        disposed_at="2025-05-01", account_id=acc, chain="BTC",
    )
    engine.match_disposal(db, dsp1, method=method)
    gain1 = _sum_match(db, dsp1, "gain_loss_usd")

    # Fee went from 0 -> 200, so gain drops by exactly 200.
    assert gain0 - gain1 == Decimal("200.000000"), (
        f"method={method}: gain0={gain0}, gain1={gain1}"
    )


def test_zero_fee_disposal_has_gross_proceeds(db):
    """fee_usd=0 / NULL still yields gross proceeds unchanged."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    dsp = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=50000, fee_usd=0,
        disposed_at="2025-05-01", account_id=acc, chain="BTC",
    )
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert Decimal(str(matches[0].proceeds_usd)) == Decimal("50000.000000")
    assert Decimal(str(matches[0].gain_loss_usd)) == Decimal("20000.000000")
