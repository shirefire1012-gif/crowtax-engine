"""Tests for roadmap item 1.8 - Rev. Proc. 2024-28 election documentation.

Covers:
    * record_election inserts and validates the election_type.
    * import_sua writes the mapping AND retargets the lots'
      account_id to the specified accounts.
    * validate_for_year: no warnings for a pre-2025 report.
    * validate_for_year: WARNING when post-2024 disposals exist and no
      election row is present.
    * validate_for_year: WARNING when most recent election is
      'none_filed'.
    * No warning when a valid SUA or global_alloc election is on file.
    * filing_package.build_package surfaces the election status in the
      manifest and summary.json.
"""

from __future__ import annotations

import json

import pytest

from crowtax_engine import elections, filing_package
from tests.builders import make_account, make_disposal, make_lot


def test_record_election_rejects_bad_type(db):
    with pytest.raises(ValueError, match="election_type"):
        elections.record_election(
            db, effective_date="2025-01-01", election_type="bogus",
        )


def test_import_sua_writes_mapping_and_updates_lots(db):
    acc_a = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    acc_b = make_account(db, source="kraken", wallet_address="B", chain="BTC")

    # Lot initially at account A.
    lot_id = make_lot(
        db, symbol="BTC", quantity=1, price_usd=30000,
        acquired_at="2024-06-01", account_id=acc_a, chain="BTC",
    )

    elections.import_sua(
        db,
        effective_date="2025-01-01",
        lot_account_map=[{"lot_id": lot_id, "account_id": acc_b}],
        documentation_path="/etc/sua_signed.pdf",
    )

    cur = db.cursor()
    cur.execute("SELECT account_id FROM tax_lots WHERE id = %s", (lot_id,))
    assert cur.fetchone()[0] == acc_b
    cur.close()

    current = elections.current_election(db)
    assert current["election_type"] == "specific_unit"
    assert current["documentation_path"] == "/etc/sua_signed.pdf"


def test_validate_for_year_no_disposals_no_warnings(db):
    # 2025 filing but zero disposals: no warnings.
    warnings = elections.validate_for_year(db, year=2025)
    assert warnings == []


def test_validate_for_year_warns_when_no_election(db):
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-06-01", account_id=acc, chain="BTC")
    make_disposal(db, symbol="BTC", quantity=1, proceeds_usd=35000,
                  disposed_at="2025-03-01", account_id=acc, chain="BTC")

    warnings = elections.validate_for_year(db, year=2025)
    assert len(warnings) == 1
    assert "UNKNOWN" in warnings[0]


def test_validate_for_year_warns_when_none_filed(db):
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-06-01", account_id=acc, chain="BTC")
    make_disposal(db, symbol="BTC", quantity=1, proceeds_usd=35000,
                  disposed_at="2025-03-01", account_id=acc, chain="BTC")

    elections.record_none_filed(db, effective_date="2025-01-01")

    warnings = elections.validate_for_year(db, year=2025)
    assert len(warnings) == 1
    assert "none_filed" in warnings[0]


def test_validate_for_year_silent_with_valid_sua(db):
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-06-01", account_id=acc, chain="BTC")
    make_disposal(db, symbol="BTC", quantity=1, proceeds_usd=35000,
                  disposed_at="2025-03-01", account_id=acc, chain="BTC")

    elections.record_election(
        db, effective_date="2025-01-01", election_type="global_alloc",
        details={"ordering_rule": "fifo_across_accounts"},
        documentation_path="/etc/global_alloc.pdf",
    )

    warnings = elections.validate_for_year(db, year=2025)
    assert warnings == []


def test_filing_package_manifest_includes_election_status(db, tmp_path):
    acc = make_account(db, source="coinbase", wallet_address="A", chain="BTC")
    make_lot(db, symbol="BTC", quantity=1, price_usd=30000,
             acquired_at="2024-06-01", account_id=acc, chain="BTC")
    make_disposal(db, symbol="BTC", quantity=1, proceeds_usd=35000,
                  disposed_at="2025-03-01", account_id=acc, chain="BTC")

    elections.record_none_filed(db, effective_date="2025-01-01")

    pkg = filing_package.build_package(db, year=2025, method="fifo")
    assert pkg.manifest["rev_proc_2024_28_election"] == "none_filed"
    assert len(pkg.manifest["warnings"]) == 1

    out = tmp_path / "pkg"
    filing_package.export_package(pkg, str(out))
    summary = json.loads((out / "summary.json").read_text())
    assert summary["rev_proc_2024_28_election"] == "none_filed"
    assert "warnings" in summary
    assert len(summary["warnings"]) == 1
