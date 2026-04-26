"""Roadmap item 2.4 - NFT classification framework tests.

Acceptance from roadmap line 341-345:
- An NFT lot tagged ``nft_collectible`` held >1 year and sold at a gain
  flows to a separate Schedule D line carrying the 28% rate flag.
- Non-collectible NFTs continue to flow with generic crypto.

Framework only - the taxpayer does not currently trade NFTs.  Default
``asset_class`` is ``'fungible'`` and the existing 65 fungible tests
continue to pass unchanged.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crowtax_engine import filing_package
from tests.builders import make_account, make_disposal, make_lot


def test_default_asset_class_is_fungible(db):
    acct = make_account(db, source="onchain", wallet_address="0xnft",
                        chain="ETH")
    lot_id = make_lot(db, symbol="ETH", quantity="1", price_usd="2000",
                      acquired_at="2022-01-01", account_id=acct,
                      wallet_address="0xnft", chain="ETH",
                      source="onchain", source_tx_id="default-fungible")
    cur = db.cursor()
    cur.execute("SELECT asset_class FROM tax_lots WHERE id = %s", (lot_id,))
    assert cur.fetchone()[0] == "fungible"
    cur.close()


def test_collectible_long_term_gain_segregated_to_28pct_line(db, tmp_path):
    """Collectible NFT held >1y at gain -> separate Schedule D line."""
    acct = make_account(db, source="onchain", wallet_address="0xnft",
                        chain="ETH", display_name="NFT Wallet")
    # Acquired 2022, sold 2024 -> long term.  Tagged collectible.
    make_lot(db, symbol="BAYC", quantity="1", price_usd="50000",
             acquired_at="2022-03-01", account_id=acct,
             wallet_address="0xnft", chain="ETH", source="onchain",
             source_tx_id="bayc-buy",
             asset_class="nft_collectible")
    make_disposal(db, symbol="BAYC", quantity="1", proceeds_usd="80000",
                  disposed_at="2024-04-01", account_id=acct,
                  wallet_address="0xnft", chain="ETH", source="onchain",
                  source_tx_id="bayc-sell")

    # Also a vanilla long-term ETH gain that must NOT touch the 28% line.
    make_lot(db, symbol="ETH", quantity="2", price_usd="1500",
             acquired_at="2022-06-01", account_id=acct,
             wallet_address="0xnft", chain="ETH", source="onchain",
             source_tx_id="eth-buy")
    make_disposal(db, symbol="ETH", quantity="2", proceeds_usd="6000",
                  disposed_at="2024-04-15", account_id=acct,
                  wallet_address="0xnft", chain="ETH", source="onchain",
                  source_tx_id="eth-sell")

    pkg = filing_package.build_package(db, year=2024, method="fifo")

    # Long-term Part II should NOT include the BAYC line; it lives in
    # the dedicated collectibles bucket.
    long_descs = [l["description"] for l in pkg.form_8949_part_ii]
    coll_descs = [l["description"] for l in pkg.form_8949_collectibles]
    assert any("ETH" in d for d in long_descs)
    assert not any("BAYC" in d for d in long_descs)
    assert any("BAYC" in d for d in coll_descs)

    # The collectibles entry carries the 28% rate flag.
    rate_lines = [
        sd for sd in pkg.schedule_d_summary if sd.rate_28pct_collectibles
    ]
    assert len(rate_lines) == 1
    assert rate_lines[0].total_gain_loss == Decimal("30000")  # 80k - 50k
    assert "28%" in rate_lines[0].description
    # The BAYC line carries asset_class on the form dict.
    assert all(l["asset_class"] == "nft_collectible"
               for l in pkg.form_8949_collectibles)

    # Schedule D net total includes BOTH the regular long-term and the
    # collectibles bucket.
    net_line = pkg.schedule_d_summary[-1]
    assert net_line.description == "Net capital gain/loss"
    assert net_line.total_gain_loss == Decimal("33000")  # 30000 + 3000

    # JSON export reflects the 28% flag and the collectibles file.
    out = tmp_path / "pkg"
    files = filing_package.export_package(pkg, str(out))
    assert "form_8949_collectibles.csv" in files
    import json
    summary = json.loads((out / "summary.json").read_text())
    assert summary["form_8949_collectibles_lines"] == 1
    rate_in_json = [
        sd for sd in summary["schedule_d"]
        if sd["rate_28pct_collectibles"]
    ]
    assert len(rate_in_json) == 1


def test_non_collectible_nft_flows_with_generic_crypto(db):
    """``nft_non_collectible`` lots stay on Form 8949 Part II / Part I."""
    acct = make_account(db, source="onchain", wallet_address="0xnft",
                        chain="ETH")
    make_lot(db, symbol="ARTBLOCKS", quantity="1", price_usd="5000",
             acquired_at="2022-05-01", account_id=acct,
             wallet_address="0xnft", chain="ETH", source="onchain",
             source_tx_id="ab-buy",
             asset_class="nft_non_collectible")
    make_disposal(db, symbol="ARTBLOCKS", quantity="1", proceeds_usd="9000",
                  disposed_at="2024-07-01", account_id=acct,
                  wallet_address="0xnft", chain="ETH", source="onchain",
                  source_tx_id="ab-sell")

    pkg = filing_package.build_package(db, year=2024, method="fifo")
    assert pkg.form_8949_collectibles == []
    long_descs = [l["description"] for l in pkg.form_8949_part_ii]
    assert any("ARTBLOCKS" in d for d in long_descs)
    # No Schedule D 28% line should be present.
    assert not any(sd.rate_28pct_collectibles for sd in pkg.schedule_d_summary)


def test_invalid_asset_class_rejected(db):
    """CHECK constraint blocks unknown asset_class values."""
    acct = make_account(db, source="onchain", wallet_address="0xnft",
                        chain="ETH")
    cur = db.cursor()
    try:
        with pytest.raises(Exception):
            cur.execute(
                """
                INSERT INTO tax_lots
                    (account_id, wallet_address, chain, symbol, acquired_at,
                     quantity, cost_basis_usd, cost_basis_per_unit,
                     remaining_quantity, acquisition_type, source,
                     asset_class)
                VALUES (%s, '0xnft', 'ETH', 'X', 1000, 1, 1, 1, 1,
                        'purchase', 'test', 'collectible_typo')
                """,
                (acct,),
            )
            db.commit()
    finally:
        db.rollback()
        cur.close()
