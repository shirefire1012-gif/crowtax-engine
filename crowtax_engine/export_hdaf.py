"""Historical Digital Asset Form (HDAF) export.

Roadmap item 2.5.  The IRS released the HDAF in March 2026 as the
disclosure form requested in active digital-asset audits.  It demands
wallet-by-wallet, exchange-by-exchange lifetime history: inflows,
outflows, current balance, and the underlying transaction manifest.

This module is **audit-only** - it is never required for normal annual
filing.  Keeping it warm (and tested) means an audit notice can be
answered in minutes rather than weeks.

Output structure (under ``--out``):

    hdaf_export/
        manifest.json                     -- top-level summary, run params
        accounts/
            <id>_<source>_<chain>_<wallet>/
                summary.json              -- lifetime totals + balance
                transactions.csv          -- all lots + disposals + transfers
                source_files.json         -- raw_transaction fingerprints

The directory tree is then zipped to ``--out`` when the path ends
``.zip``; otherwise it is left in place.

CLI:

    python -m tax.export_hdaf --start 2020-01-01 --out /tmp/hdaf.zip
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import psycopg2.extras

log = logging.getLogger(__name__)


def _epoch_from_date(s: str) -> int:
    return int(
        datetime.strptime(s, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _epoch_to_iso(epoch: Optional[int]) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _slug(s: Optional[str]) -> str:
    """Filesystem-safe slug for account directory names."""
    if not s:
        return "none"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return cleaned.strip("_") or "none"


def _decimal_to_str(d) -> str:
    if d is None:
        return "0"
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    return format(d, "f")


def _list_accounts(conn) -> list[dict]:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, source, wallet_address, chain, display_name, created_at
            FROM tax_accounts
            ORDER BY id ASC
            """
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _account_lots(conn, account_id, start_epoch, end_epoch):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, symbol, acquired_at, quantity, cost_basis_usd,
                   cost_basis_per_unit, remaining_quantity,
                   acquisition_type, fee_usd, source, source_tx_id,
                   raw_transaction_id, transfer_id, parent_lot_id
            FROM tax_lots
            WHERE account_id = %s
              AND acquired_at BETWEEN %s AND %s
            ORDER BY acquired_at ASC, id ASC
            """,
            (account_id, start_epoch, end_epoch),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _account_disposals(conn, account_id, start_epoch, end_epoch):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, symbol, disposed_at, quantity, proceeds_usd,
                   fee_usd, source, source_tx_id, raw_transaction_id,
                   wash_sale_flag
            FROM tax_disposals
            WHERE account_id = %s
              AND disposed_at BETWEEN %s AND %s
            ORDER BY disposed_at ASC, id ASC
            """,
            (account_id, start_epoch, end_epoch),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _account_transfers(conn, account_id, start_epoch, end_epoch):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, from_account_id, to_account_id, symbol, quantity,
                   transferred_at, fee_usd, status, raw_transaction_id,
                   paired_raw_transaction_id
            FROM tax_transfers
            WHERE (from_account_id = %s OR to_account_id = %s)
              AND transferred_at BETWEEN %s AND %s
            ORDER BY transferred_at ASC, id ASC
            """,
            (account_id, account_id, start_epoch, end_epoch),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _raw_transaction_fingerprints(conn, raw_ids):
    if not raw_ids:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, source, source_file, chain, tx_hash, block_number,
                   timestamp, status
            FROM tax_raw_transactions
            WHERE id = ANY(%s)
            ORDER BY id ASC
            """,
            (list(raw_ids),),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _build_summary(account, lots, disposals, transfers):
    """Lifetime inflow / outflow / balance per symbol."""
    by_symbol = {}

    def _bucket(sym):
        return by_symbol.setdefault(sym, {
            "inflow_qty": Decimal(0),
            "outflow_qty": Decimal(0),
            "inflow_usd": Decimal(0),
            "outflow_usd": Decimal(0),
            "current_balance": Decimal(0),
            "transfer_in_qty": Decimal(0),
            "transfer_out_qty": Decimal(0),
        })

    for lot in lots:
        b = _bucket(lot["symbol"])
        b["inflow_qty"] += Decimal(str(lot["quantity"]))
        b["inflow_usd"] += Decimal(str(lot["cost_basis_usd"]))
        b["current_balance"] += Decimal(str(lot["remaining_quantity"]))

    for d in disposals:
        b = _bucket(d["symbol"])
        b["outflow_qty"] += Decimal(str(d["quantity"]))
        b["outflow_usd"] += Decimal(str(d["proceeds_usd"]))

    for t in transfers:
        b = _bucket(t["symbol"])
        qty = Decimal(str(t["quantity"]))
        if t["from_account_id"] == account["id"]:
            b["transfer_out_qty"] += qty
        if t["to_account_id"] == account["id"]:
            b["transfer_in_qty"] += qty

    return {
        "account_id": account["id"],
        "source": account["source"],
        "wallet_address": account["wallet_address"],
        "chain": account["chain"],
        "display_name": account.get("display_name"),
        "lifetime_totals": {
            sym: {k: _decimal_to_str(v) for k, v in vals.items()}
            for sym, vals in sorted(by_symbol.items())
        },
        "lot_count": len(lots),
        "disposal_count": len(disposals),
        "transfer_count": len(transfers),
    }


def _write_transactions_csv(path, lots, disposals, transfers, account_id):
    fieldnames = [
        "kind", "id", "symbol", "occurred_at", "quantity",
        "usd_amount", "fee_usd", "source", "source_tx_id",
        "raw_transaction_id", "details",
    ]
    rows = []
    for lot in lots:
        rows.append({
            "kind": "lot",
            "id": lot["id"],
            "symbol": lot["symbol"],
            "occurred_at": _epoch_to_iso(lot["acquired_at"]),
            "quantity": _decimal_to_str(lot["quantity"]),
            "usd_amount": _decimal_to_str(lot["cost_basis_usd"]),
            "fee_usd": _decimal_to_str(lot.get("fee_usd")),
            "source": lot["source"],
            "source_tx_id": lot.get("source_tx_id") or "",
            "raw_transaction_id": lot.get("raw_transaction_id") or "",
            "details": json.dumps({
                "acquisition_type": lot["acquisition_type"],
                "remaining_quantity": _decimal_to_str(
                    lot["remaining_quantity"]),
                "transfer_id": lot.get("transfer_id"),
                "parent_lot_id": lot.get("parent_lot_id"),
            }),
        })
    for d in disposals:
        rows.append({
            "kind": "disposal",
            "id": d["id"],
            "symbol": d["symbol"],
            "occurred_at": _epoch_to_iso(d["disposed_at"]),
            "quantity": _decimal_to_str(d["quantity"]),
            "usd_amount": _decimal_to_str(d["proceeds_usd"]),
            "fee_usd": _decimal_to_str(d.get("fee_usd")),
            "source": d["source"],
            "source_tx_id": d.get("source_tx_id") or "",
            "raw_transaction_id": d.get("raw_transaction_id") or "",
            "details": json.dumps({
                "wash_sale_flag": bool(d.get("wash_sale_flag")),
            }),
        })
    for t in transfers:
        if t["from_account_id"] == account_id:
            direction = "out"
        elif t["to_account_id"] == account_id:
            direction = "in"
        else:
            direction = "unmatched"
        rows.append({
            "kind": f"transfer_{direction}",
            "id": t["id"],
            "symbol": t["symbol"],
            "occurred_at": _epoch_to_iso(t["transferred_at"]),
            "quantity": _decimal_to_str(t["quantity"]),
            "usd_amount": "",
            "fee_usd": _decimal_to_str(t.get("fee_usd")),
            "source": "transfer",
            "source_tx_id": "",
            "raw_transaction_id": t.get("raw_transaction_id") or "",
            "details": json.dumps({
                "from_account_id": t["from_account_id"],
                "to_account_id": t["to_account_id"],
                "status": t["status"],
            }),
        })
    rows.sort(key=lambda r: r["occurred_at"])
    with path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export(conn, *, start_epoch, end_epoch, out_path):
    """Walk every ``tax_accounts`` row and emit per-account artefacts."""
    accounts = _list_accounts(conn)
    workdir = Path(tempfile.mkdtemp(prefix="hdaf_"))
    try:
        accounts_dir = workdir / "accounts"
        accounts_dir.mkdir(parents=True, exist_ok=True)
        per_account = []
        for acct in accounts:
            slug = (
                f"{acct['id']:04d}_"
                f"{_slug(acct['source'])}_"
                f"{_slug(acct['chain'])}_"
                f"{_slug(acct['wallet_address'])}"
            )
            adir = accounts_dir / slug
            adir.mkdir(parents=True, exist_ok=True)

            lots = _account_lots(conn, acct["id"], start_epoch, end_epoch)
            disposals = _account_disposals(
                conn, acct["id"], start_epoch, end_epoch)
            transfers = _account_transfers(
                conn, acct["id"], start_epoch, end_epoch)

            summary = _build_summary(acct, lots, disposals, transfers)
            (adir / "summary.json").write_text(
                json.dumps(summary, indent=2, default=str))

            _write_transactions_csv(
                adir / "transactions.csv",
                lots, disposals, transfers, acct["id"])

            raw_ids = set()
            for row in (*lots, *disposals, *transfers):
                rid = row.get("raw_transaction_id")
                if rid is not None:
                    raw_ids.add(int(rid))
                paired = row.get("paired_raw_transaction_id")
                if paired is not None:
                    raw_ids.add(int(paired))
            fingerprints = _raw_transaction_fingerprints(conn, raw_ids)
            for fp_row in fingerprints:
                fp_row["timestamp_iso"] = _epoch_to_iso(
                    fp_row.get("timestamp"))
            (adir / "source_files.json").write_text(json.dumps({
                "raw_transaction_count": len(fingerprints),
                "raw_transactions": fingerprints,
            }, indent=2, default=str))

            per_account.append({
                "account_id": acct["id"],
                "directory": f"accounts/{slug}",
                "lot_count": len(lots),
                "disposal_count": len(disposals),
                "transfer_count": len(transfers),
            })

        manifest = {
            "form": "HDAF",
            "generated_at": datetime.now(tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "start_date": _epoch_to_iso(start_epoch),
            "end_date": _epoch_to_iso(end_epoch),
            "account_count": len(accounts),
            "accounts": per_account,
        }
        (workdir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str))

        out_path = Path(out_path)
        if str(out_path).lower().endswith(".zip"):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if out_path.exists():
                out_path.unlink()
            with zipfile.ZipFile(out_path, "w",
                                 compression=zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(workdir):
                    for fname in files:
                        full = Path(root) / fname
                        zf.write(full, arcname=full.relative_to(workdir))
        else:
            out_path.mkdir(parents=True, exist_ok=True)
            for child in workdir.iterdir():
                target = out_path / child.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(child), str(target))

        return manifest
    finally:
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export the Historical Digital Asset Form (HDAF) "
                    "audit package."
    )
    parser.add_argument("--start", required=True,
                        help="inclusive start date YYYY-MM-DD")
    parser.add_argument("--end", default=None,
                        help="inclusive end date YYYY-MM-DD "
                             "(default: today)")
    parser.add_argument("--out", required=True,
                        help="output path; .zip -> zipfile, "
                             "otherwise a directory")
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN override")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start_epoch = _epoch_from_date(args.start)
    if args.end:
        end_epoch = _epoch_from_date(args.end) + (24 * 60 * 60 - 1)
    else:
        end_epoch = int(datetime.now(tz=timezone.utc).timestamp())

    if args.dsn is None:
        from crowtax_engine.db import get_conn
        conn = get_conn()
    else:
        import psycopg2
        conn = psycopg2.connect(args.dsn)

    try:
        manifest = export(
            conn,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            out_path=Path(args.out),
        )
    finally:
        conn.close()

    print(json.dumps({
        "out": args.out,
        "account_count": manifest["account_count"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
