"""1099-DA reconciliation against engine-computed disposals.

Roadmap item 2.1.  Compares each tax_1099da_lines row to the best
matching tax_disposals row and proposes Form 8949 column (f)
adjustment codes per file 1 sec 2.1.

Match rules:
    * account_id when broker line carries one, else any account.
    * Symbol equality (case-insensitive).
    * disposed_at within +/- 1 day.
    * Quantity within +/- 0.1% relative.

Form 8949 column (f) adjustment codes (2025 instructions):
    * Code B -- basis incorrectly reported on 1099-B/DA;
      column (g) = signed delta (reported - engine).
    * Code O -- catch-all when broker reported no basis but engine
      has one; column (g) is null and an explanatory note is
      attached to the line.
Multiple codes per line are allowed and emitted in alphabetical order.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import psycopg2.extras

log = logging.getLogger(__name__)

QUANTITY_REL_TOL = Decimal("0.001")
DATE_TOL = timedelta(days=1)
BASIS_DELTA_THRESHOLD_USD = Decimal("1.00")


@dataclass
class ReconciliationResult:
    broker_line_id: Optional[int]
    engine_disposal_id: Optional[int]
    status: str
    reported_proceeds: Optional[Decimal]
    engine_proceeds: Optional[Decimal]
    reported_basis: Optional[Decimal]
    engine_basis: Optional[Decimal]
    proceeds_delta: Optional[Decimal]
    basis_delta: Optional[Decimal]
    proposed_8949_code: str = ""
    proposed_8949_amount: Optional[Decimal] = None
    note: str = ""

    def as_csv_row(self):
        return {
            "broker_line_id": self.broker_line_id or "",
            "engine_disposal_id": self.engine_disposal_id or "",
            "status": self.status,
            "reported_proceeds": _fmt(self.reported_proceeds),
            "engine_proceeds": _fmt(self.engine_proceeds),
            "reported_basis": _fmt(self.reported_basis),
            "engine_basis": _fmt(self.engine_basis),
            "basis_delta": _fmt(self.basis_delta),
            "proceeds_delta": _fmt(self.proceeds_delta),
            "proposed_8949_code": self.proposed_8949_code,
            "proposed_8949_amount": _fmt(self.proposed_8949_amount),
            "note": self.note,
        }


def _fmt(v):
    if v is None:
        return ""
    return f"{Decimal(str(v)):.6f}"


def _engine_basis_for_disposal(conn, disposal_id):
    """Sum of cost_basis_usd across this disposal's lot matches.

    Returns None when the disposal has not been matched yet.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COALESCE(SUM(cost_basis_usd), 0), COUNT(*) "
            "FROM tax_lot_matches WHERE disposal_id = %s",
            (disposal_id,),
        )
        total, n = cur.fetchone()
        if n == 0:
            return None
        return Decimal(str(total))
    finally:
        cur.close()


def _quantity_within_tol(a, b):
    a = Decimal(str(a))
    b = Decimal(str(b))
    base = max(abs(a), abs(b))
    if base == 0:
        return True
    return (abs(a - b) / base) <= QUANTITY_REL_TOL


def _find_engine_match(conn, line):
    """Best engine disposal for a broker line, or None.

    Selection: same symbol, disposed_at within DATE_TOL, quantity
    within QUANTITY_REL_TOL.  account_id is matched when the broker
    line has one.  Closest-in-time wins among candidates.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        line_dt = line["disposed_at"]
        if isinstance(line_dt, datetime):
            line_epoch = int(line_dt.astimezone(timezone.utc).timestamp())
        else:
            line_epoch = int(line_dt)
        epoch_lo = line_epoch - int(DATE_TOL.total_seconds())
        epoch_hi = line_epoch + int(DATE_TOL.total_seconds())

        params = [line["symbol"].upper(), epoch_lo, epoch_hi]
        sql = (
            "SELECT * FROM tax_disposals "
            "WHERE UPPER(symbol) = %s "
            "  AND disposed_at BETWEEN %s AND %s"
        )
        if line.get("account_id") is not None:
            sql += " AND account_id = %s"
            params.append(line["account_id"])
        cur.execute(sql, params)
        candidates = cur.fetchall()

        best = None
        best_dt_delta = None
        for c in candidates:
            if not _quantity_within_tol(c["quantity"], line["quantity"]):
                continue
            dt_delta = abs(int(c["disposed_at"]) - line_epoch)
            if best_dt_delta is None or dt_delta < best_dt_delta:
                best = c
                best_dt_delta = dt_delta
        return dict(best) if best else None
    finally:
        cur.close()


def _propose_codes(reported_basis, engine_basis):
    """Return (code_string, amount).

    * If reported basis is None and engine has one, propose 'O' with
      no amount (note attached at caller).
    * If both present and abs(delta) > BASIS_DELTA_THRESHOLD_USD,
      propose 'B' with amount = reported - engine (signed).
    * Multiple codes concatenated alphabetically.
    """
    codes = []
    amount = None

    if reported_basis is None and engine_basis is not None:
        codes.append("O")

    if reported_basis is not None and engine_basis is not None:
        delta = Decimal(str(reported_basis)) - Decimal(str(engine_basis))
        if abs(delta) > BASIS_DELTA_THRESHOLD_USD:
            codes.append("B")
            amount = delta

    return "".join(sorted(codes)), amount


def reconcile(conn, *, broker_id=None, form_year=None):
    """Reconcile broker-reported lines against engine disposals.

    Returns a list of ReconciliationResult covering:

        * matched lines (status='matched' or 'delta')
        * lines with no engine counterpart (unmatched_broker)
        * engine disposals with no broker counterpart (unmatched_engine)
          -- limited to broker account scope when broker_id given.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        clauses, params = [], []
        if broker_id is not None:
            clauses.append("broker_id = %s")
            params.append(broker_id)
        if form_year is not None:
            clauses.append("form_year = %s")
            params.append(form_year)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(
            f"SELECT * FROM tax_1099da_lines {where} "
            "ORDER BY disposed_at ASC, id ASC",
            params,
        )
        broker_lines = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()

    results = []
    matched_disposal_ids = set()
    broker_account_ids = set()

    for line in broker_lines:
        if line.get("account_id") is not None:
            broker_account_ids.add(line["account_id"])

        match = _find_engine_match(conn, line)
        if match is None:
            results.append(ReconciliationResult(
                broker_line_id=line["id"],
                engine_disposal_id=None,
                status="unmatched_broker",
                reported_proceeds=Decimal(str(line["proceeds_usd"])),
                engine_proceeds=None,
                reported_basis=(Decimal(str(line["basis_usd"]))
                                if line["basis_usd"] is not None else None),
                engine_basis=None,
                proceeds_delta=None,
                basis_delta=None,
                note="no engine disposal found within tolerance window",
            ))
            continue

        matched_disposal_ids.add(match["id"])
        engine_basis = _engine_basis_for_disposal(conn, match["id"])
        engine_proceeds = Decimal(str(match["proceeds_usd"])) - Decimal(
            str(match.get("fee_usd") or 0))
        reported_proceeds = Decimal(str(line["proceeds_usd"]))
        reported_basis = (Decimal(str(line["basis_usd"]))
                          if line["basis_usd"] is not None else None)

        proceeds_delta = reported_proceeds - engine_proceeds
        basis_delta = (reported_basis - engine_basis
                       if (reported_basis is not None
                           and engine_basis is not None)
                       else None)

        code, amount = _propose_codes(reported_basis, engine_basis)
        note = ""
        if "O" in code:
            note = ("broker did not report basis; engine basis used. "
                    "Consider attaching support to Form 8949.")

        is_delta = bool(code) or abs(proceeds_delta) > Decimal("0.01")
        status = "delta" if is_delta else "matched"

        results.append(ReconciliationResult(
            broker_line_id=line["id"],
            engine_disposal_id=match["id"],
            status=status,
            reported_proceeds=reported_proceeds,
            engine_proceeds=engine_proceeds,
            reported_basis=reported_basis,
            engine_basis=engine_basis,
            proceeds_delta=proceeds_delta,
            basis_delta=basis_delta,
            proposed_8949_code=code,
            proposed_8949_amount=amount,
            note=note,
        ))

    if broker_id is not None and broker_account_ids:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            placeholders = ",".join(["%s"] * len(broker_account_ids))
            cur.execute(
                f"SELECT * FROM tax_disposals "
                f"WHERE account_id IN ({placeholders})",
                tuple(broker_account_ids),
            )
            engine_disposals = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()

        for d in engine_disposals:
            if d["id"] in matched_disposal_ids:
                continue
            engine_basis = _engine_basis_for_disposal(conn, d["id"])
            engine_proceeds = Decimal(str(d["proceeds_usd"])) - Decimal(
                str(d.get("fee_usd") or 0))
            results.append(ReconciliationResult(
                broker_line_id=None,
                engine_disposal_id=d["id"],
                status="unmatched_engine",
                reported_proceeds=None,
                engine_proceeds=engine_proceeds,
                reported_basis=None,
                engine_basis=engine_basis,
                proceeds_delta=None,
                basis_delta=None,
                note="engine has disposal without 1099-DA line",
            ))

    return results


CSV_HEADER = [
    "broker_line_id", "engine_disposal_id", "status",
    "reported_proceeds", "engine_proceeds",
    "reported_basis", "engine_basis",
    "basis_delta", "proceeds_delta",
    "proposed_8949_code", "proposed_8949_amount", "note",
]


def write_csv(results, out_path):
    """Write a reconciliation_report.csv at out_path."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        for r in results:
            writer.writerow(r.as_csv_row())
    return path


def summary_counts(results):
    """Return a dict of status -> count for manifest embedding."""
    out = {"matched": 0, "delta": 0, "unmatched_broker": 0,
           "unmatched_engine": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out
