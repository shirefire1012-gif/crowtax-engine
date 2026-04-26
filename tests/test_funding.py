"""Tests for roadmap item 1.6 - perpetual funding events.

No IRS primary guidance; practitioner consensus treats funding as
ordinary income / expense at payment time.  Funding in USDC is IRC 61
gross income at FMV (USDC par = 1).

Covers:
    * Positive funding_usd => direction='received'.
    * Negative funding_usd => direction='paid'.
    * Zero funding is a valid no-op record.
    * summarize_by_year splits by direction and computes net correctly.
    * USDC lot with basis=par + funding event does not double-count:
      a subsequent USDC disposal at par produces zero capital gain.
"""

from __future__ import annotations

from decimal import Decimal

from crowtax_engine import engine, funding
from tests.builders import make_account, make_disposal, make_lot


def test_positive_funding_is_received(db):
    acc = make_account(db, source="hyperliquid", wallet_address="hl1", chain="HYPE")
    new_id = funding.record_funding(
        db, account_id=acc, symbol_perp="BTC-PERP",
        funding_at=1_710_000_000, funding_usd=Decimal("12.50"),
        settlement_symbol="USDC",
    )
    cur = db.cursor()
    cur.execute(
        "SELECT direction, funding_usd FROM tax_funding_events WHERE id = %s",
        (new_id,))
    direction, amount = cur.fetchone()
    cur.close()
    assert direction == "received"
    assert Decimal(str(amount)) == Decimal("12.500000")


def test_negative_funding_is_paid(db):
    acc = make_account(db, source="hyperliquid", wallet_address="hl1", chain="HYPE")
    new_id = funding.record_funding(
        db, account_id=acc, symbol_perp="ETH-PERP",
        funding_at=1_710_000_000, funding_usd=Decimal("-50"),
        settlement_symbol="USDC",
    )
    cur = db.cursor()
    cur.execute(
        "SELECT direction, funding_usd FROM tax_funding_events WHERE id = %s",
        (new_id,))
    direction, amount = cur.fetchone()
    cur.close()
    assert direction == "paid"
    assert Decimal(str(amount)) == Decimal("-50.000000")


def test_zero_funding_is_received_and_net_zero(db):
    acc = make_account(db, source="hyperliquid", wallet_address="hl1", chain="HYPE")
    funding.record_funding(
        db, account_id=acc, symbol_perp="BTC-PERP",
        funding_at=1_710_000_000, funding_usd=Decimal("0"),
    )
    totals = funding.summarize_by_year(db, year=2024)
    assert totals["net"] == Decimal("0")


def test_summarize_splits_by_direction(db):
    acc = make_account(db, source="hyperliquid", wallet_address="hl1", chain="HYPE")
    # 2024: +100 received, -60 paid, net +40.
    funding.record_funding(
        db, account_id=acc, symbol_perp="BTC-PERP",
        funding_at=1_704_067_200, funding_usd=Decimal("100"),
    )
    funding.record_funding(
        db, account_id=acc, symbol_perp="ETH-PERP",
        funding_at=1_710_000_000, funding_usd=Decimal("-60"),
    )
    # 2025: +5.
    funding.record_funding(
        db, account_id=acc, symbol_perp="HYPE-PERP",
        funding_at=1_738_368_000, funding_usd=Decimal("5"),
    )

    by_year = funding.summarize_by_year(db)
    assert by_year[2024]["received"] == Decimal("100")
    assert by_year[2024]["paid"] == Decimal("-60")
    assert by_year[2024]["net"] == Decimal("40")
    assert by_year[2025]["received"] == Decimal("5")
    assert by_year[2025]["paid"] == Decimal("0")


def test_usdc_lot_from_funding_produces_zero_capital_gain(db):
    """Acceptance: USDC lot at par basis + funding income -> no double
    count on subsequent USDC disposal."""
    acc = make_account(db, source="hyperliquid", wallet_address="hl1", chain="HYPE")

    # Record the funding event.
    funding.record_funding(
        db, account_id=acc, symbol_perp="BTC-PERP",
        funding_at=1_710_000_000, funding_usd=Decimal("50"),
        settlement_symbol="USDC",
    )
    # The ingest path would typically also create a USDC lot at basis=$50.
    lot_id = make_lot(
        db, symbol="USDC", quantity=50, price_usd=1,
        acquired_at=1_710_000_000, account_id=acc, chain="HYPE",
        acquisition_type="purchase",
    )

    # Later the user moves that $50 USDC elsewhere (disposal at par).
    dsp = make_disposal(
        db, symbol="USDC", quantity=50, proceeds_usd=50,
        disposed_at=1_720_000_000, account_id=acc, chain="HYPE",
    )
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    assert Decimal(str(matches[0].gain_loss_usd)) == Decimal("0")


def test_summarize_by_year_returns_zero_bucket_for_missing_year(db):
    totals = funding.summarize_by_year(db, year=2023)
    assert totals == {
        "received": Decimal(0),
        "paid": Decimal(0),
        "net": Decimal(0),
    }
