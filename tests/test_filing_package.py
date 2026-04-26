"""Tests for roadmap item 1.7 - structured FilingPackage output.

Covers:
    * build_package produces separate Part I (short) / Part II (long)
      line lists that match the underlying report.
    * Schedule D totals reconcile to Part I + Part II sums.
    * Schedule 1 ordinary income incorporates mining/staking/airdrop/fork
      (1.5) and funding received/paid (1.6).
    * NC D-400 AGI contribution = capital gain + ordinary income +
      net funding.
    * export_package writes exactly the required file set.
    * summary.json totals reconcile to the in-memory package.
"""

from __future__ import annotations

import json
from decimal import Decimal

from crowtax_engine import filing_package, funding, ordinary_income
from tests.builders import make_account, make_disposal, make_lot


def _seed_2024_trades(db):
    """Two 2024 disposals: one short-term gain, one long-term gain."""
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    # Short-term: acquired 2024-01, sold 2024-06.
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-01-01", account_id=acc, chain="BTC")
    make_disposal(db, symbol="BTC", quantity=1, proceeds_usd=35000,
                  disposed_at="2024-06-01", account_id=acc, chain="BTC")
    # Long-term: acquired 2022-01, sold 2024-12.
    make_lot(db, symbol="ETH", quantity=2, price_usd=1500,
             acquired_at="2022-01-01", account_id=acc, chain="ETH")
    make_disposal(db, symbol="ETH", quantity=2, proceeds_usd=4200,
                  disposed_at="2024-12-01", account_id=acc, chain="ETH")
    return acc


def test_build_package_splits_short_and_long_term(db):
    _seed_2024_trades(db)
    pkg = filing_package.build_package(db, year=2024, method="fifo")

    assert pkg.year == 2024
    assert pkg.method == "fifo"
    assert len(pkg.form_8949_part_i) == 1   # short-term BTC
    assert len(pkg.form_8949_part_ii) == 1  # long-term ETH


def test_schedule_d_reconciles_to_form_8949(db):
    _seed_2024_trades(db)
    pkg = filing_package.build_package(db, year=2024, method="fifo")

    short_line = pkg.schedule_d_summary[0]
    long_line = pkg.schedule_d_summary[1]
    net_line = pkg.schedule_d_summary[2]

    # Reconciliation invariant.
    assert net_line.total_proceeds == short_line.total_proceeds + long_line.total_proceeds
    assert net_line.total_cost_basis == short_line.total_cost_basis + long_line.total_cost_basis
    assert net_line.total_gain_loss == short_line.total_gain_loss + long_line.total_gain_loss

    # Short-term: $35k proceeds, $30k basis => $5k gain.
    assert short_line.total_gain_loss == Decimal("5000.00")
    # Long-term: $4200 proceeds, $3000 basis => $1200 gain.
    assert long_line.total_gain_loss == Decimal("1200.00")
    # Net = $6200.
    assert net_line.total_gain_loss == Decimal("6200.00")


def test_schedule_1_includes_ordinary_income_and_funding(db):
    acc = _seed_2024_trades(db)

    ordinary_income.record_income(
        db, income_type="staking", symbol="ETH", quantity="1",
        received_at=1_717_200_000,  # June 2024
        fmv_usd=Decimal("500"), account_id=acc,
    )
    ordinary_income.record_income(
        db, income_type="airdrop", symbol="OP", quantity="10",
        received_at=1_717_200_000,
        fmv_usd=Decimal("30"), account_id=acc,
    )
    funding.record_funding(
        db, account_id=acc, symbol_perp="BTC-PERP",
        funding_at=1_717_200_000, funding_usd=Decimal("25"),
    )

    pkg = filing_package.build_package(db, year=2024, method="fifo")
    assert pkg.schedule_1_ordinary_income_by_type["staking"] == Decimal("500")
    assert pkg.schedule_1_ordinary_income_by_type["airdrop"] == Decimal("30")
    assert pkg.schedule_1_ordinary_income_by_type["mining"] == Decimal("0")
    assert pkg.schedule_1_ordinary_income_by_type["fork"] == Decimal("0")
    assert pkg.funding_summary["received"] == Decimal("25")
    assert pkg.funding_summary["net"] == Decimal("25")


def test_nc_d400_agi_contribution_sums_all_income_streams(db):
    acc = _seed_2024_trades(db)
    ordinary_income.record_income(
        db, income_type="staking", symbol="ETH", quantity="1",
        received_at=1_717_200_000,
        fmv_usd=Decimal("500"), account_id=acc,
    )
    funding.record_funding(
        db, account_id=acc, symbol_perp="BTC-PERP",
        funding_at=1_717_200_000, funding_usd=Decimal("-100"),
    )

    pkg = filing_package.build_package(db, year=2024, method="fifo")
    # Capital gain: 6200 (from test above).  Ordinary: 500.  Funding: -100.
    # AGI contribution: 6200 + 500 + (-100) = 6600.
    assert pkg.nc_d400_agi_contribution == Decimal("6600.00")


def test_export_package_writes_required_file_set(db, tmp_path):
    _seed_2024_trades(db)
    pkg = filing_package.build_package(db, year=2024, method="fifo")
    out = tmp_path / "2024_package"
    written = filing_package.export_package(pkg, str(out))

    expected = {
        "form_8949_part_i.csv",
        "form_8949_part_ii.csv",
        "form_8949_collectibles.csv",  # roadmap 2.4 - separate 28% line
        "schedule_d_summary.csv",
        "schedule_1_ordinary.csv",
        "nc_d400_agi.json",
        "summary.json",
        "manifest.json",
    }
    assert set(written.keys()) == expected
    for name in expected:
        assert (out / name).exists(), f"Missing {name}"


def test_summary_json_reconciles_to_package(db, tmp_path):
    _seed_2024_trades(db)
    pkg = filing_package.build_package(db, year=2024, method="fifo")
    out = tmp_path / "2024_package"
    filing_package.export_package(pkg, str(out))

    summary = json.loads((out / "summary.json").read_text())
    assert summary["year"] == 2024
    assert summary["method"] == "fifo"
    assert summary["form_8949_part_i_lines"] == 1
    assert summary["form_8949_part_ii_lines"] == 1
    # Schedule D net line matches package.
    net = summary["schedule_d"][2]
    assert Decimal(net["total_gain_loss"]) == Decimal("6200.00")
    assert Decimal(summary["nc_d400_agi_contribution_usd"]) == Decimal("6200.00")
    # manifest has the right shape.
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["year"] == 2024
    assert "generated_at" in manifest
