"""Roadmap item 2.5 - HDAF export tests.

Acceptance from roadmap line 367: a 3-account fixture produces three
per-account directories with correct lifetime totals + transactions.csv
+ source_files.json.
"""

from __future__ import annotations

import csv
import json
import zipfile
from decimal import Decimal

from crowtax_engine import export_hdaf
from tests.builders import make_account, make_disposal, make_lot


def _seed_three_accounts(db):
    """3 accounts: cb spot, hl perp, on-chain. Mix of lots / disposals."""
    cb = make_account(db, source="coinbase",
                      wallet_address="cb-handle", chain="none",
                      display_name="Coinbase Spot")
    hl = make_account(db, source="hyperliquid",
                      wallet_address="hl-handle", chain="HYPE",
                      display_name="Hyperliquid Perp")
    eth = make_account(db, source="onchain",
                       wallet_address="0xabc0000000000000000000000000000000000001",
                       chain="ETH",
                       display_name="ETH wallet")

    # Coinbase: bought BTC 1.0 @ $30k, sold 0.4 @ $40k.
    make_lot(db, symbol="BTC", quantity="1.0", price_usd="30000",
             acquired_at="2024-01-15", account_id=cb,
             wallet_address="cb-handle", chain="none", source="coinbase")
    make_disposal(db, symbol="BTC", quantity="0.4", proceeds_usd="16000",
                  disposed_at="2024-06-01", account_id=cb,
                  wallet_address="cb-handle", chain="none",
                  source="coinbase")

    # HL: bought USDC 50k @ $1, no disposals.
    make_lot(db, symbol="USDC", quantity="50000", price_usd="1",
             acquired_at="2024-02-10", account_id=hl,
             wallet_address="hl-handle", chain="HYPE",
             source="hyperliquid")

    # On-chain: bought ETH 5 @ $2000, disposed 1 @ $3000.
    make_lot(db, symbol="ETH", quantity="5", price_usd="2000",
             acquired_at="2024-03-12", account_id=eth,
             wallet_address="0xabc0000000000000000000000000000000000001",
             chain="ETH", source="onchain")
    make_disposal(db, symbol="ETH", quantity="1", proceeds_usd="3000",
                  disposed_at="2024-09-01", account_id=eth,
                  wallet_address="0xabc0000000000000000000000000000000000001",
                  chain="ETH", source="onchain")
    return cb, hl, eth


def test_export_three_accounts_to_directory(db, tmp_path):
    cb, hl, eth = _seed_three_accounts(db)

    out = tmp_path / "hdaf_out"
    manifest = export_hdaf.export(
        db,
        start_epoch=export_hdaf._epoch_from_date("2024-01-01"),
        end_epoch=export_hdaf._epoch_from_date("2024-12-31") + 86399,
        out_path=out,
    )

    assert manifest["account_count"] == 3
    accounts_dir = out / "accounts"
    dirs = sorted(p.name for p in accounts_dir.iterdir())
    assert len(dirs) == 3

    # Verify Coinbase numbers
    cb_dir = next(p for p in accounts_dir.iterdir() if "coinbase" in p.name)
    summary = json.loads((cb_dir / "summary.json").read_text())
    btc = summary["lifetime_totals"]["BTC"]
    assert Decimal(btc["inflow_qty"]) == Decimal("1.0")
    assert Decimal(btc["inflow_usd"]) == Decimal("30000")
    assert Decimal(btc["outflow_qty"]) == Decimal("0.4")
    assert Decimal(btc["outflow_usd"]) == Decimal("16000")
    assert Decimal(btc["current_balance"]) == Decimal("1.0")  # remaining_qty

    # transactions.csv has both rows for Coinbase.
    with (cb_dir / "transactions.csv").open() as fp:
        rows = list(csv.DictReader(fp))
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["disposal", "lot"]

    # HL is lot-only (no disposal row).
    hl_dir = next(p for p in accounts_dir.iterdir() if "hyperliquid" in p.name)
    hl_summary = json.loads((hl_dir / "summary.json").read_text())
    usdc = hl_summary["lifetime_totals"]["USDC"]
    assert Decimal(usdc["inflow_qty"]) == Decimal("50000")
    assert hl_summary["disposal_count"] == 0

    # ETH on-chain
    eth_dir = next(p for p in accounts_dir.iterdir() if "onchain" in p.name)
    eth_summary = json.loads((eth_dir / "summary.json").read_text())
    eth_totals = eth_summary["lifetime_totals"]["ETH"]
    assert Decimal(eth_totals["inflow_qty"]) == Decimal("5")
    assert Decimal(eth_totals["outflow_qty"]) == Decimal("1")
    assert Decimal(eth_totals["outflow_usd"]) == Decimal("3000")

    # Top-level manifest.json
    top_manifest = json.loads((out / "manifest.json").read_text())
    assert top_manifest["form"] == "HDAF"
    assert top_manifest["account_count"] == 3
    assert len(top_manifest["accounts"]) == 3


def test_export_to_zip(db, tmp_path):
    _seed_three_accounts(db)
    zip_path = tmp_path / "hdaf.zip"
    manifest = export_hdaf.export(
        db,
        start_epoch=export_hdaf._epoch_from_date("2024-01-01"),
        end_epoch=export_hdaf._epoch_from_date("2024-12-31") + 86399,
        out_path=zip_path,
    )
    assert zip_path.exists()
    assert manifest["account_count"] == 3

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    # Must contain top-level manifest plus per-account files.
    assert "manifest.json" in names
    assert any(n.endswith("/summary.json") for n in names)
    assert any(n.endswith("/transactions.csv") for n in names)
    assert any(n.endswith("/source_files.json") for n in names)
    # Three accounts -> three of each.
    assert sum(1 for n in names if n.endswith("/summary.json")) == 3


def test_export_respects_date_window(db, tmp_path):
    """Lots / disposals outside the window must not appear in summary."""
    cb, _, _ = _seed_three_accounts(db)
    # Add a 2023 disposal for Coinbase that should NOT be in the 2024 window.
    make_disposal(db, symbol="BTC", quantity="0.1", proceeds_usd="5000",
                  disposed_at="2023-08-01", account_id=cb,
                  wallet_address="cb-handle", chain="none",
                  source="coinbase", source_tx_id="cb-2023-out-1")

    out = tmp_path / "windowed"
    export_hdaf.export(
        db,
        start_epoch=export_hdaf._epoch_from_date("2024-01-01"),
        end_epoch=export_hdaf._epoch_from_date("2024-12-31") + 86399,
        out_path=out,
    )
    cb_dir = next(p for p in (out / "accounts").iterdir()
                  if "coinbase" in p.name)
    summary = json.loads((cb_dir / "summary.json").read_text())
    assert Decimal(summary["lifetime_totals"]["BTC"]["outflow_qty"]) == \
        Decimal("0.4")  # the 2023 row was excluded
