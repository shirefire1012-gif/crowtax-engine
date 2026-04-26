"""Tests for roadmap item 1.4 - wash-sale detection vs application split.

IRC section 1091 reaches "stock or securities"; digital assets are
property under Notice 2014-21.  Default engine posture is to DETECT
the pattern but NOT APPLY the basis adjustment.

Covers:
    * Default (APPLY_WASH_SALE_ADJUSTMENT=False): pattern is detected,
      wash_sale_pattern_detected=TRUE, wash_sale_flag stays FALSE,
      tax_lots.cost_basis_usd is NOT modified, gain/loss is unchanged.
    * Apply-mode (flag flipped True) reproduces the pre-1.4 adjustment:
      basis shifted into replacement lot, wash_sale_flag=TRUE, loss
      disallowed on the disposal.
    * rematch_all reports both detection count and application count.
    * A gain (not loss) never detects the pattern.
"""

from __future__ import annotations

from decimal import Decimal

from crowtax_engine import engine
from tests.builders import make_account, make_disposal, make_lot


def _lot_basis(conn, lot_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT cost_basis_usd, wash_sale_basis_adjustment "
            "FROM tax_lots WHERE id = %s", (lot_id,))
        return cur.fetchone()
    finally:
        cur.close()


def _disposal_flags(conn, disposal_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT wash_sale_pattern_detected, wash_sale_flag, "
            "wash_sale_disallowed_loss FROM tax_disposals WHERE id = %s",
            (disposal_id,))
        return cur.fetchone()
    finally:
        cur.close()


def test_detect_only_flags_pattern_without_adjusting_basis(db):
    """Default posture: detect, record, but do not alter basis."""
    assert engine.APPLY_WASH_SALE_ADJUSTMENT is False

    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    loss_lot = make_lot(
        db, symbol="BTC", quantity=1, price_usd=50000,
        acquired_at="2024-01-01", account_id=acc, chain="BTC",
    )
    # Sell at a loss: 1 BTC for $40k, basis was $50k -> -$10k loss.
    dsp = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=40000,
        disposed_at="2024-02-01", account_id=acc, chain="BTC",
    )
    # Repurchase within 30 days - this is the wash-sale trigger.
    replacement_lot = make_lot(
        db, symbol="BTC", quantity=1, price_usd=42000,
        acquired_at="2024-02-15", account_id=acc, chain="BTC",
    )

    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    gain_before_wash = Decimal(str(matches[0].gain_loss_usd))
    assert gain_before_wash == Decimal("-10000.000000")

    detected = engine.check_wash_sales(db, "BTC", dsp)
    assert detected is True

    pattern, flag, disallowed = _disposal_flags(db, dsp)
    assert pattern is True
    assert flag is False
    assert Decimal(str(disallowed)) == Decimal("0")

    # Replacement lot basis is unchanged.
    basis, adj = _lot_basis(db, replacement_lot)
    assert Decimal(str(basis)) == Decimal("42000.000000")
    assert Decimal(str(adj)) == Decimal("0")


def test_apply_mode_reproduces_legacy_adjustment(db, monkeypatch):
    """Flipping APPLY_WASH_SALE_ADJUSTMENT=True reproduces prior math."""
    monkeypatch.setattr(engine, "APPLY_WASH_SALE_ADJUSTMENT", True)

    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=50000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    dsp = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=40000,
        disposed_at="2024-02-01", account_id=acc, chain="BTC",
    )
    replacement_lot = make_lot(
        db, symbol="BTC", quantity=1, price_usd=42000,
        acquired_at="2024-02-15", account_id=acc, chain="BTC",
    )

    engine.match_disposal(db, dsp, method="fifo")
    engine.check_wash_sales(db, "BTC", dsp)

    pattern, flag, disallowed = _disposal_flags(db, dsp)
    assert pattern is True
    assert flag is True
    # Full $10k loss disallowed (1 BTC fully replaced).
    assert Decimal(str(disallowed)) == Decimal("10000.000000")

    # Replacement lot basis shifts up by the disallowed amount.
    basis, adj = _lot_basis(db, replacement_lot)
    assert Decimal(str(basis)) == Decimal("52000.000000")
    assert Decimal(str(adj)) == Decimal("10000.000000")


def test_rematch_all_reports_detection_and_application_separately(db):
    """Summary dict surfaces both detection count and apply count."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=50000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=40000,
        disposed_at="2024-02-01", account_id=acc, chain="BTC",
    )
    make_lot(db, symbol="BTC", quantity=1, price_usd=42000,
             acquired_at="2024-02-15", account_id=acc, chain="BTC")

    summary = engine.rematch_all(db, method="fifo")
    assert summary["wash_sale_pattern_detected"] == 1
    assert summary["wash_sale_applied"] == 0  # detect-only default
    assert summary["wash_sale_policy"] == "detect_only"


def test_gain_disposal_does_not_trigger_pattern(db):
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    dsp = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=40000,  # gain
        disposed_at="2024-02-01", account_id=acc, chain="BTC",
    )
    make_lot(db, symbol="BTC", quantity=1, price_usd=42000,
             acquired_at="2024-02-15", account_id=acc, chain="BTC")

    engine.match_disposal(db, dsp, method="fifo")
    detected = engine.check_wash_sales(db, "BTC", dsp)
    assert detected is False

    pattern, flag, disallowed = _disposal_flags(db, dsp)
    assert pattern is False
    assert flag is False


def test_rematch_all_clears_stale_pattern_flag(db, monkeypatch):
    """A prior-run pattern flag is cleared at the start of rematch_all."""
    monkeypatch.setattr(engine, "APPLY_WASH_SALE_ADJUSTMENT", False)

    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=50000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    dsp = make_disposal(
        db, symbol="BTC", quantity=1, proceeds_usd=40000,
        disposed_at="2024-02-01", account_id=acc, chain="BTC",
    )
    make_lot(db, symbol="BTC", quantity=1, price_usd=42000,
             acquired_at="2024-02-15", account_id=acc, chain="BTC")

    engine.rematch_all(db, method="fifo")
    pattern, _, _ = _disposal_flags(db, dsp)
    assert pattern is True

    # Simulate "operator removes the repurchase" by deleting the
    # replacement lot, then rematch - flag must clear.
    cur = db.cursor()
    cur.execute("DELETE FROM tax_lots WHERE cost_basis_usd = 42000")
    db.commit()
    cur.close()

    engine.rematch_all(db, method="fifo")
    pattern, _, _ = _disposal_flags(db, dsp)
    assert pattern is False
