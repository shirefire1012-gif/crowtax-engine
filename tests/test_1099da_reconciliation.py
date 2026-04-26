"""Tests for roadmap item 2.1 -- 1099-DA ingest + reconciliation.

IRS Form 1099-DA (Treas. Decn 10000, July 2024) requires custodial
digital-asset brokers to report gross proceeds beginning 2025 and
adjusted basis beginning 2026.  The engine ingests broker CSVs and
reconciles each line against its own disposal ledger, proposing
Form 8949 column (f) adjustment codes for any disagreement.
"""

from __future__ import annotations

import csv
from decimal import Decimal

from crowtax_engine import ingest_1099da, reconcile_1099da
from tests.builders import (
    make_account,
    make_disposal,
    make_lot,
)

COINBASE_MAPPING = {
    "broker_id": "coinbase",
    "form_year": 2025,
    "columns": {
        "payee_id": "account_id",
        "proceeds_usd": "gross_proceeds",
        "basis_usd": "cost_basis",
        "acquisition_date": "date_acquired",
        "disposed_at": "date_sold",
        "symbol": "asset",
        "quantity": "qty",
        "covered_status": "covered_flag",
    },
}


def _write_csv(tmp_path, rows, name="coinbase_1099da.csv"):
    p = tmp_path / name
    headers = list(rows[0].keys())
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def _epoch(s):
    from datetime import datetime, timezone
    return int(datetime.strptime(s, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp())


def _make_engine_disposal(db, account_id, *, symbol, qty,
                          proceeds, date_str, basis_per_unit=None,
                          chain="ETH"):
    """Helper: lot + matching disposal + lot match (so engine_basis
    is reconcilable).
    """
    if basis_per_unit is None:
        basis_per_unit = Decimal(str(proceeds)) / Decimal(str(qty))
    lot_id = make_lot(
        db, symbol=symbol, quantity=qty, price_usd=basis_per_unit,
        acquired_at="2024-01-15", account_id=account_id, chain=chain,
    )
    dsp_id = make_disposal(
        db, symbol=symbol, quantity=qty, proceeds_usd=proceeds,
        disposed_at=date_str, account_id=account_id, chain=chain,
    )
    cur = db.cursor()
    cost_basis = Decimal(str(qty)) * Decimal(str(basis_per_unit))
    gain = Decimal(str(proceeds)) - cost_basis
    cur.execute(
        """
        INSERT INTO tax_lot_matches
            (disposal_id, lot_id, quantity_matched, cost_basis_usd,
             proceeds_usd, gain_loss_usd, holding_period, method)
        VALUES (%s, %s, %s, %s, %s, %s, 'long', 'fifo')
        """,
        (dsp_id, lot_id, qty, cost_basis, proceeds, gain),
    )
    db.commit()
    cur.close()
    return lot_id, dsp_id


def test_parse_csv_basic(tmp_path):
    rows = [{
        "account_id": "PAYEE-001",
        "gross_proceeds": "5000.00",
        "cost_basis": "3000.00",
        "date_acquired": "2024-01-15",
        "date_sold": "2025-06-10",
        "asset": "BTC",
        "qty": "0.1",
        "covered_flag": "covered",
    }]
    csv_path = _write_csv(tmp_path, rows)
    parsed = ingest_1099da.parse_csv(csv_path, COINBASE_MAPPING)
    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "BTC"
    assert parsed[0]["proceeds_usd"] == Decimal("5000.00")
    assert parsed[0]["basis_usd"] == Decimal("3000.00")
    assert parsed[0]["covered_status"] == "covered"


def test_ingest_csv_inserts_rows(db, tmp_path):
    acc = make_account(db, source="coinbase",
                       wallet_address="cb-default", chain="none")
    rows = [
        {
            "account_id": "P1",
            "gross_proceeds": "5000.00",
            "cost_basis": "3000.00",
            "date_acquired": "2024-01-15",
            "date_sold": "2025-06-10",
            "asset": "BTC",
            "qty": "0.1",
            "covered_flag": "covered",
        },
        {
            "account_id": "P1",
            "gross_proceeds": "1500.00",
            "cost_basis": "",
            "date_acquired": "",
            "date_sold": "2025-08-22",
            "asset": "ETH",
            "qty": "0.5",
            "covered_flag": "noncovered",
        },
    ]
    csv_path = _write_csv(tmp_path, rows)
    n = ingest_1099da.ingest_csv(
        db, csv_path, COINBASE_MAPPING, account_id=acc)
    assert n == 2

    # Idempotent: re-ingest same file inserts zero new rows.
    n2 = ingest_1099da.ingest_csv(
        db, csv_path, COINBASE_MAPPING, account_id=acc)
    assert n2 == 0

    lines = ingest_1099da.list_lines(
        db, broker_id="coinbase", form_year=2025)
    assert len(lines) == 2
    eth = [l for l in lines if l["symbol"] == "ETH"][0]
    assert eth["basis_usd"] is None
    assert eth["covered_status"] == "noncovered"


def test_reconcile_all_matched(db, tmp_path):
    """5 lines all matching engine disposals -> 100% reconciled."""
    acc = make_account(db, source="coinbase",
                       wallet_address="cb-default", chain="none")
    rows = []
    for i in range(5):
        date_str = f"2025-06-{10+i:02d}"
        proceeds = Decimal("500") * (i + 1)
        basis = Decimal("300") * (i + 1)
        qty = Decimal("0.01") * (i + 1)
        rows.append({
            "account_id": "P1",
            "gross_proceeds": str(proceeds),
            "cost_basis": str(basis),
            "date_acquired": "2024-01-15",
            "date_sold": date_str,
            "asset": "BTC",
            "qty": str(qty),
            "covered_flag": "covered",
        })
        _make_engine_disposal(
            db, acc, symbol="BTC", qty=qty,
            proceeds=proceeds, date_str=date_str,
            basis_per_unit=(basis / qty),
        )
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(
        db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(
        db, broker_id="coinbase", form_year=2025)
    counts = reconcile_1099da.summary_counts(results)
    assert counts["matched"] == 5
    assert counts["delta"] == 0
    assert counts["unmatched_broker"] == 0
    assert counts["unmatched_engine"] == 0


def test_reconcile_basis_delta_proposes_code_b(db, tmp_path):
    """Basis delta of $50 -> status=delta, code=B, amount signed."""
    acc = make_account(db, source="coinbase",
                       wallet_address="cb-default", chain="none")
    # Engine basis = $3000; broker reports $3050 (over-reported by $50).
    _make_engine_disposal(
        db, acc, symbol="BTC", qty=Decimal("0.1"),
        proceeds=Decimal("5000"), date_str="2025-06-10",
        basis_per_unit=Decimal("30000"),
    )
    rows = [{
        "account_id": "P1",
        "gross_proceeds": "5000.00",
        "cost_basis": "3050.00",
        "date_acquired": "2024-01-15",
        "date_sold": "2025-06-10",
        "asset": "BTC",
        "qty": "0.1",
        "covered_flag": "covered",
    }]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(
        db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(
        db, broker_id="coinbase", form_year=2025)
    matched = [r for r in results
               if r.engine_disposal_id is not None]
    assert len(matched) == 1
    r = matched[0]
    assert r.status == "delta"
    assert r.proposed_8949_code == "B"
    assert r.proposed_8949_amount == Decimal("50.00")
    assert r.basis_delta == Decimal("50.00")


def test_reconcile_unmatched_broker_line(db, tmp_path):
    """Reported but no engine match -> unmatched_broker, listed."""
    acc = make_account(db, source="coinbase",
                       wallet_address="cb-default", chain="none")
    rows = [{
        "account_id": "P1",
        "gross_proceeds": "5000.00",
        "cost_basis": "3000.00",
        "date_acquired": "2024-01-15",
        "date_sold": "2025-06-10",
        "asset": "BTC",
        "qty": "0.1",
        "covered_flag": "covered",
    }]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(
        db, csv_path, COINBASE_MAPPING, account_id=acc)

    # No engine disposal at all.
    results = reconcile_1099da.reconcile(
        db, broker_id="coinbase", form_year=2025)
    assert len(results) == 1
    assert results[0].status == "unmatched_broker"
    assert results[0].engine_disposal_id is None


def test_reconcile_unmatched_engine_disposal(db, tmp_path):
    """Engine disposal with no broker match -> unmatched_engine."""
    acc = make_account(db, source="coinbase",
                       wallet_address="cb-default", chain="none")
    # Engine has TWO disposals; broker only reports one.
    _make_engine_disposal(
        db, acc, symbol="BTC", qty=Decimal("0.1"),
        proceeds=Decimal("5000"), date_str="2025-06-10",
        basis_per_unit=Decimal("30000"),
    )
    _make_engine_disposal(
        db, acc, symbol="BTC", qty=Decimal("0.2"),
        proceeds=Decimal("10000"), date_str="2025-09-15",
        basis_per_unit=Decimal("30000"),
    )
    rows = [{
        "account_id": "P1",
        "gross_proceeds": "5000.00",
        "cost_basis": "3000.00",
        "date_acquired": "2024-01-15",
        "date_sold": "2025-06-10",
        "asset": "BTC",
        "qty": "0.1",
        "covered_flag": "covered",
    }]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(
        db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(
        db, broker_id="coinbase", form_year=2025)
    counts = reconcile_1099da.summary_counts(results)
    assert counts["matched"] == 1
    assert counts["unmatched_engine"] == 1


def test_quantity_tolerance(db, tmp_path):
    acc = make_account(db, source='coinbase',
                       wallet_address='cb-default', chain='none')
    _make_engine_disposal(
        db, acc, symbol='ETH', qty=Decimal('1.0'),
        proceeds=Decimal('3000'), date_str='2025-06-10',
        basis_per_unit=Decimal('2000'),
    )
    _make_engine_disposal(
        db, acc, symbol='LINK', qty=Decimal('1.0'),
        proceeds=Decimal('100'), date_str='2025-07-10',
        basis_per_unit=Decimal('60'),
    )
    rows = [
        {'account_id': 'P1', 'gross_proceeds': '3000.00',
         'cost_basis': '2000.00', 'date_acquired': '2024-01-15',
         'date_sold': '2025-06-10', 'asset': 'ETH',
         'qty': '1.0001', 'covered_flag': 'covered'},
        {'account_id': 'P1', 'gross_proceeds': '100.00',
         'cost_basis': '60.00', 'date_acquired': '2024-01-15',
         'date_sold': '2025-07-10', 'asset': 'LINK',
         'qty': '1.002', 'covered_flag': 'covered'},
    ]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(db, broker_id='coinbase', form_year=2025)
    counts = reconcile_1099da.summary_counts(results)
    assert counts['matched'] == 1
    assert counts['unmatched_broker'] == 1
    assert counts['unmatched_engine'] == 1


def test_date_tolerance(db, tmp_path):
    acc = make_account(db, source='coinbase',
                       wallet_address='cb-default', chain='none')
    _make_engine_disposal(
        db, acc, symbol='ETH', qty=Decimal('1.0'),
        proceeds=Decimal('3000'), date_str='2025-06-10',
        basis_per_unit=Decimal('2000'),
    )
    _make_engine_disposal(
        db, acc, symbol='BTC', qty=Decimal('0.1'),
        proceeds=Decimal('5000'), date_str='2025-07-10',
        basis_per_unit=Decimal('30000'),
    )
    rows = [
        {'account_id': 'P1', 'gross_proceeds': '3000.00',
         'cost_basis': '2000.00', 'date_acquired': '2024-01-15',
         'date_sold': '2025-06-10', 'asset': 'ETH',
         'qty': '1.0', 'covered_flag': 'covered'},
        {'account_id': 'P1', 'gross_proceeds': '5000.00',
         'cost_basis': '3000.00', 'date_acquired': '2024-01-15',
         'date_sold': '2025-07-12', 'asset': 'BTC',
         'qty': '0.1', 'covered_flag': 'covered'},
    ]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(db, broker_id='coinbase', form_year=2025)
    counts = reconcile_1099da.summary_counts(results)
    assert counts['matched'] == 1
    assert counts['unmatched_broker'] == 1
    assert counts['unmatched_engine'] == 1


def test_reconcile_broker_no_basis_engine_has_basis(db, tmp_path):
    acc = make_account(db, source='coinbase',
                       wallet_address='cb-default', chain='none')
    _make_engine_disposal(
        db, acc, symbol='BTC', qty=Decimal('0.1'),
        proceeds=Decimal('5000'), date_str='2025-06-10',
        basis_per_unit=Decimal('30000'),
    )
    rows = [{'account_id': 'P1', 'gross_proceeds': '5000.00',
             'cost_basis': '', 'date_acquired': '',
             'date_sold': '2025-06-10', 'asset': 'BTC',
             'qty': '0.1', 'covered_flag': 'noncovered'}]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(db, broker_id='coinbase', form_year=2025)
    matched = [r for r in results if r.engine_disposal_id is not None]
    assert len(matched) == 1
    assert matched[0].proposed_8949_code == 'O'
    assert matched[0].status == 'delta'
    assert 'broker did not report basis' in matched[0].note


def test_write_csv_emits_audit_report(db, tmp_path):
    acc = make_account(db, source='coinbase',
                       wallet_address='cb-default', chain='none')
    _make_engine_disposal(
        db, acc, symbol='BTC', qty=Decimal('0.1'),
        proceeds=Decimal('5000'), date_str='2025-06-10',
        basis_per_unit=Decimal('30000'),
    )
    rows = [{'account_id': 'P1', 'gross_proceeds': '5000.00',
             'cost_basis': '3050.00', 'date_acquired': '2024-01-15',
             'date_sold': '2025-06-10', 'asset': 'BTC',
             'qty': '0.1', 'covered_flag': 'covered'}]
    csv_path = _write_csv(tmp_path, rows)
    ingest_1099da.ingest_csv(db, csv_path, COINBASE_MAPPING, account_id=acc)

    results = reconcile_1099da.reconcile(db, broker_id='coinbase', form_year=2025)
    out = tmp_path / 'reconciliation_report.csv'
    reconcile_1099da.write_csv(results, out)
    assert out.exists()
    with open(out) as fh:
        reader = csv.DictReader(fh)
        rows_out = list(reader)
    assert len(rows_out) >= 1
    assert rows_out[0]['proposed_8949_code'] == 'B'
