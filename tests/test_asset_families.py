"""Roadmap item 2.2 - stable / wrap realization-event tests.

Acceptance from roadmap line 291-295:
- USDC->USDT 1:1 swap of $10,000 produces a disposal row with $0 gain.
- A USDC->USDT swap during a depeg at $0.97 produces the correct loss.
- Suppression option reduces Form 8949 line count without changing
  Schedule D totals.

The migration 008 seeds USDC, USDT, DAI, USDP, BUSD, TUSD into
``usd_stable``; BTC, WBTC, cbBTC into ``btc_wrap``; ETH, WETH, stETH
into ``eth_wrap``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crowtax_engine import asset_families, report
from tests.builders import make_account, make_disposal, make_lot

_SEED_ROWS = [
    ("usd_stable", "USDC"),
    ("usd_stable", "USDT"),
    ("usd_stable", "DAI"),
    ("usd_stable", "USDP"),
    ("usd_stable", "BUSD"),
    ("usd_stable", "TUSD"),
    ("btc_wrap",   "BTC"),
    ("btc_wrap",   "WBTC"),
    ("btc_wrap",   "cbBTC"),
    ("eth_wrap",   "ETH"),
    ("eth_wrap",   "WETH"),
    ("eth_wrap",   "stETH"),
]


@pytest.fixture(autouse=True)
def _reseed_families(db):
    """The tests/conftest.py truncates every ``tax_*`` table per test;
    that wipes the family seed installed by migration 008.  Re-insert
    here so each test gets the canonical lookup."""
    asset_families.reset_cache()
    cur = db.cursor()
    try:
        cur.executemany(
            "INSERT INTO tax_asset_families (family, symbol) "
            "VALUES (%s, %s) ON CONFLICT (symbol) DO NOTHING",
            _SEED_ROWS,
        )
        db.commit()
    finally:
        cur.close()
    asset_families.reset_cache()
    yield
    asset_families.reset_cache()


def test_seeded_families(db):
    assert asset_families.get_family(db, "USDC") == "usd_stable"
    assert asset_families.get_family(db, "USDT") == "usd_stable"
    assert asset_families.get_family(db, "DAI") == "usd_stable"
    assert asset_families.get_family(db, "WBTC") == "btc_wrap"
    assert asset_families.get_family(db, "BTC") == "btc_wrap"
    assert asset_families.get_family(db, "ETH") == "eth_wrap"
    assert asset_families.get_family(db, "WETH") == "eth_wrap"
    # Case-insensitive
    assert asset_families.get_family(db, "usdc") == "usd_stable"
    # Out-of-family
    assert asset_families.get_family(db, "SOL") is None


def test_is_same_family_swap(db):
    assert asset_families.is_same_family_swap(db, "USDC", "USDT") == "usd_stable"
    assert asset_families.is_same_family_swap(db, "BTC", "WBTC") == "btc_wrap"
    assert asset_families.is_same_family_swap(db, "ETH", "WETH") == "eth_wrap"
    # Cross-family - NOT a wrap.
    assert asset_families.is_same_family_swap(db, "USDC", "ETH") is None
    assert asset_families.is_same_family_swap(db, "BTC", "ETH") is None
    # Unknown asset returns None even if other side is in a family.
    assert asset_families.is_same_family_swap(db, "USDC", "WIF") is None


def _seed_stable_swap(db, *, sell_qty, sell_price, buy_qty, buy_price,
                      wrap_family, account=None):
    """Seed a USDC->USDT swap as a disposal+lot pair, both annotated."""
    if account is None:
        account = make_account(db, source="onchain",
                               wallet_address="0xtest", chain="ETH")
    # USDC sold @ sell_price (FMV)
    lot_usdc = make_lot(db, symbol="USDC", quantity=sell_qty,
                        price_usd="1.00", acquired_at="2024-01-01",
                        account_id=account, wallet_address="0xtest",
                        chain="ETH", source="onchain",
                        source_tx_id="usdc-buy")
    disp = make_disposal(db, symbol="USDC", quantity=sell_qty,
                         proceeds_usd=str(Decimal(sell_qty)
                                          * Decimal(sell_price)),
                         disposed_at="2024-06-01",
                         account_id=account, wallet_address="0xtest",
                         chain="ETH", source="onchain",
                         source_tx_id="usdc-sell")
    # USDT acquired @ buy_price (FMV)
    lot_usdt = make_lot(db, symbol="USDT", quantity=buy_qty,
                        price_usd=str(buy_price),
                        acquired_at="2024-06-01",
                        account_id=account, wallet_address="0xtest",
                        chain="ETH", source="onchain",
                        source_tx_id="usdt-buy")
    asset_families.annotate_lot_disposal_pair(db, lot_usdt, disp, wrap_family)
    return account, disp, lot_usdc, lot_usdt


def test_usdc_to_usdt_at_par_zero_gain(db):
    """USDC -> USDT at $1.00 each: zero gain, line still present."""
    _seed_stable_swap(db, sell_qty="10000", sell_price="1.00",
                      buy_qty="10000", buy_price="1.00",
                      wrap_family="usd_stable")
    rep = report.generate_report(db, year=2024, method="fifo")
    # All wrap-stable swaps are short term here.
    assert len(rep.short_term_items) == 1
    line = rep.short_term_items[0]
    assert line.wrap_family == "usd_stable"
    assert abs(line.gain_loss) < 0.01
    assert rep.short_term_total.num_transactions == 1
    assert abs(rep.short_term_total.total_gain_loss) < 0.01


def test_usdc_to_usdt_during_depeg_real_loss(db):
    """USDC sold @ $0.97 during depeg vs basis $1.00: real loss reported."""
    _seed_stable_swap(db, sell_qty="10000", sell_price="0.97",
                      buy_qty="10000", buy_price="0.97",
                      wrap_family="usd_stable")
    rep = report.generate_report(db, year=2024, method="fifo")
    line = rep.short_term_items[0]
    assert line.wrap_family == "usd_stable"
    # Loss = 10000 * (0.97 - 1.00) = -$300
    assert line.gain_loss == pytest.approx(-300.0, abs=0.01)


def test_suppression_changes_line_count_not_totals(db):
    """--suppress-zero-swaps hides on-peg lines but keeps Schedule D total."""
    # Seed two wrap swaps: one at par (zero gain) and one depegged ($300 loss).
    acct = make_account(db, source="onchain",
                        wallet_address="0xtest", chain="ETH")
    _seed_stable_swap(db, sell_qty="5000", sell_price="1.00",
                      buy_qty="5000", buy_price="1.00",
                      wrap_family="usd_stable", account=acct)
    # Second swap with depeg - need different source_tx_ids.
    lot2 = make_lot(db, symbol="USDC", quantity="5000",
                    price_usd="1.00", acquired_at="2024-02-01",
                    account_id=acct, wallet_address="0xtest",
                    chain="ETH", source="onchain",
                    source_tx_id="usdc-buy-2")
    disp2 = make_disposal(db, symbol="USDC", quantity="5000",
                          proceeds_usd="4850",  # $0.97 each
                          disposed_at="2024-07-01",
                          account_id=acct, wallet_address="0xtest",
                          chain="ETH", source="onchain",
                          source_tx_id="usdc-sell-2")
    lot2_usdt = make_lot(db, symbol="USDT", quantity="5000",
                         price_usd="0.97", acquired_at="2024-07-01",
                         account_id=acct, wallet_address="0xtest",
                         chain="ETH", source="onchain",
                         source_tx_id="usdt-buy-2")
    asset_families.annotate_lot_disposal_pair(
        db, lot2_usdt, disp2, "usd_stable")

    # Also a non-wrap disposal that must NEVER be suppressed.
    make_lot(db, symbol="ETH", quantity="1", price_usd="2000",
             acquired_at="2024-01-15", account_id=acct,
             wallet_address="0xtest", chain="ETH", source="onchain",
             source_tx_id="eth-buy")
    make_disposal(db, symbol="ETH", quantity="1", proceeds_usd="2500",
                  disposed_at="2024-08-01", account_id=acct,
                  wallet_address="0xtest", chain="ETH", source="onchain",
                  source_tx_id="eth-sell")

    rep_full = report.generate_report(db, year=2024, method="fifo")
    rep_suppressed = report.generate_report(
        db, year=2024, method="fifo",
        suppress_zero_swaps=Decimal("0.01"),
    )

    # Total count: 2 wraps + 1 ETH disposal = 3 lines without suppression.
    assert len(rep_full.short_term_items) == 3
    # With suppression, the on-peg wrap is hidden but the depegged loss
    # (>$0.01) and the ETH gain remain.
    assert len(rep_suppressed.short_term_items) == 2
    # Schedule D totals must be identical.
    assert rep_full.short_term_total.total_gain_loss == \
        rep_suppressed.short_term_total.total_gain_loss
    assert rep_full.short_term_total.total_proceeds == \
        rep_suppressed.short_term_total.total_proceeds
    assert rep_full.short_term_total.total_cost_basis == \
        rep_suppressed.short_term_total.total_cost_basis
    # num_transactions counts ALL events for Schedule D reconciliation.
    assert rep_full.short_term_total.num_transactions == \
        rep_suppressed.short_term_total.num_transactions == 3

    # Make sure the depegged loss survived suppression.
    suppressed_kinds = sorted(
        (i.wrap_family or "fungible", round(i.gain_loss, 2))
        for i in rep_suppressed.short_term_items
    )
    assert ("fungible", 500.0) in suppressed_kinds  # ETH gain
    assert ("usd_stable", -150.0) in suppressed_kinds  # depeg loss
