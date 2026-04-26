"""Tests for roadmap item 1.5 - ordinary-income acquisition types.

Notice 2014-21 Q-8 (mining), Rev. Rul. 2023-14 (staking), Rev. Rul.
2019-24 (airdrops and hard forks) - each is ordinary income at FMV on
receipt with basis in the acquired coins set to that FMV.

Covers:
    * record_income inserts a row and returns the id.
    * Stablecoin auto-fills FMV at $1/unit.
    * Missing FMV on a non-stable flags needs_review without zero-
      defaulting silently.
    * recognize_for_lot updates the lot's basis to the FMV and creates
      the income row.
    * A later sale of the income coins at +$X produces $X capital gain
      (NOT FMV+X), proving basis was set to FMV on receipt.
    * summarize_by_year_and_type returns correct totals grouped by year
      and income_type.
    * Non-ordinary-income acquisition types are no-ops for
      recognize_for_lot.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crowtax_engine import engine, ordinary_income
from tests.builders import make_account, make_disposal, make_lot


def test_record_income_happy_path(db):
    acc = make_account(db, source="onchain", wallet_address="0xaaa", chain="ETH")

    new_id = ordinary_income.record_income(
        db,
        income_type="staking",
        symbol="ETH",
        quantity="2",
        received_at=1_700_000_000,
        fmv_usd=Decimal("4000"),
        fmv_source="price_oracle",
        account_id=acc,
    )
    cur = db.cursor()
    cur.execute(
        "SELECT fmv_usd, fmv_per_unit, income_type, needs_review "
        "FROM tax_ordinary_income WHERE id = %s", (new_id,))
    fmv, per_unit, kind, review = cur.fetchone()
    cur.close()
    assert Decimal(str(fmv)) == Decimal("4000.000000")
    assert Decimal(str(per_unit)) == Decimal("2000.000000")
    assert kind == "staking"
    assert review is False


def test_stablecoin_income_auto_fmv_at_par(db):
    acc = make_account(db, source="onchain", wallet_address="0xaaa", chain="ETH")
    new_id = ordinary_income.record_income(
        db,
        income_type="staking",
        symbol="USDC",
        quantity="100",
        received_at=1_700_000_000,
        account_id=acc,
        # fmv_usd intentionally omitted
    )
    cur = db.cursor()
    cur.execute(
        "SELECT fmv_usd, fmv_source, needs_review "
        "FROM tax_ordinary_income WHERE id = %s", (new_id,))
    fmv, source, review = cur.fetchone()
    cur.close()
    assert Decimal(str(fmv)) == Decimal("100.000000")
    assert source == "stablecoin_par"
    assert review is False


def test_missing_fmv_on_non_stable_flags_review(db):
    acc = make_account(db, source="onchain", wallet_address="0xaaa", chain="ETH")
    new_id = ordinary_income.record_income(
        db,
        income_type="airdrop",
        symbol="MYSTERYCOIN",
        quantity="10000",
        received_at=1_700_000_000,
        account_id=acc,
    )
    cur = db.cursor()
    cur.execute(
        "SELECT fmv_usd, needs_review, fmv_source "
        "FROM tax_ordinary_income WHERE id = %s", (new_id,))
    fmv, review, source = cur.fetchone()
    cur.close()
    assert Decimal(str(fmv)) == Decimal("0")
    assert review is True
    assert source == "missing"


def test_record_income_rejects_unknown_type(db):
    with pytest.raises(ValueError, match="income_type"):
        ordinary_income.record_income(
            db, income_type="bogus", symbol="ETH", quantity="1",
            received_at=1_700_000_000, fmv_usd=Decimal("100"),
        )


def test_recognize_for_lot_sets_basis_to_fmv(db):
    """Staking reward lot: basis is set to FMV, not to whatever the
    ingest path wrote."""
    acc = make_account(db, source="onchain", wallet_address="0xaaa", chain="HYPE")
    # Ingest wrote the lot at a stale price (e.g. $0, airdrop at ingest
    # time with no oracle).
    lot_id = make_lot(
        db, symbol="HYPE", quantity=10, price_usd=0,
        acquired_at="2024-06-01", account_id=acc, chain="HYPE",
        acquisition_type="staking",
    )
    # Oracle later fills in $5/HYPE FMV.
    new_id = ordinary_income.recognize_for_lot(
        db, lot_id, fmv_per_unit=Decimal("5"), fmv_source="price_oracle",
    )
    assert new_id is not None

    cur = db.cursor()
    cur.execute(
        "SELECT cost_basis_usd, cost_basis_per_unit FROM tax_lots "
        "WHERE id = %s", (lot_id,))
    basis, per_unit = cur.fetchone()
    cur.close()
    assert Decimal(str(basis)) == Decimal("50.000000")
    assert Decimal(str(per_unit)) == Decimal("5.000000")


def test_sell_of_income_coin_gains_only_on_appreciation(db):
    """Acceptance criterion 1 / 2: 10 HYPE at $5 FMV = $50 income / $50
    basis; selling later at $7 = $20 capital gain (not $70)."""
    acc = make_account(db, source="onchain", wallet_address="0xaaa", chain="HYPE")
    lot_id = make_lot(
        db, symbol="HYPE", quantity=10, price_usd=0,
        acquired_at="2024-06-01", account_id=acc, chain="HYPE",
        acquisition_type="staking",
    )
    ordinary_income.recognize_for_lot(
        db, lot_id, fmv_per_unit=Decimal("5"),
    )

    dsp = make_disposal(
        db, symbol="HYPE", quantity=10, proceeds_usd=70,
        disposed_at="2025-06-01", account_id=acc, chain="HYPE",
    )
    matches = engine.match_disposal(db, dsp, method="fifo")
    assert len(matches) == 1
    assert Decimal(str(matches[0].gain_loss_usd)) == Decimal("20.000000")


def test_summarize_by_year_and_type(db):
    acc = make_account(db, source="onchain", wallet_address="0xaaa", chain="ETH")

    # 2024: $100 staking + $50 airdrop.
    ordinary_income.record_income(
        db, income_type="staking", symbol="ETH", quantity="1",
        received_at=1_704_067_200,  # 2024-01-01
        fmv_usd=Decimal("100"), account_id=acc,
    )
    ordinary_income.record_income(
        db, income_type="airdrop", symbol="OP", quantity="10",
        received_at=1_717_200_000,  # 2024-06-01ish
        fmv_usd=Decimal("50"), account_id=acc,
    )
    # 2025: $200 staking.
    ordinary_income.record_income(
        db, income_type="staking", symbol="ETH", quantity="2",
        received_at=1_738_368_000,  # 2025-02-01
        fmv_usd=Decimal("200"), account_id=acc,
    )

    totals = ordinary_income.summarize_by_year_and_type(db)
    assert totals[2024]["staking"] == Decimal("100")
    assert totals[2024]["airdrop"] == Decimal("50")
    assert totals[2025]["staking"] == Decimal("200")

    single_year = ordinary_income.summarize_by_year_and_type(db, year=2024)
    assert single_year["staking"] == Decimal("100")
    assert single_year["airdrop"] == Decimal("50")


def test_recognize_for_lot_noop_for_purchase(db):
    """A purchase lot is not ordinary income; recognize_for_lot is a
    no-op."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="ETH")
    lot_id = make_lot(
        db, symbol="ETH", quantity=1, price_usd=3000,
        acquired_at="2024-06-01", account_id=acc, chain="ETH",
        acquisition_type="purchase",
    )
    result = ordinary_income.recognize_for_lot(db, lot_id)
    assert result is None

    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM tax_ordinary_income")
    assert cur.fetchone()[0] == 0
    cur.close()


def test_review_queue_surfaces_zero_fmv(db):
    ordinary_income.record_income(
        db, income_type="airdrop", symbol="MYSTERY", quantity="100",
        received_at=1_700_000_000,
    )
    ordinary_income.record_income(
        db, income_type="staking", symbol="ETH", quantity="1",
        received_at=1_700_000_000, fmv_usd=Decimal("2000"),
    )
    queue = ordinary_income.list_review_queue(db)
    assert len(queue) == 1
    assert queue[0]["symbol"] == "MYSTERY"
