"""1099-DA broker reporting ingest (parser-only, CSV).

Roadmap item 2.1.  IRS Form 1099-DA (final regs Treas. Decn 10000,
July 2024) requires custodial digital-asset brokers to report gross
proceeds beginning with the 2025 tax year and adjusted basis beginning
2026.  Per file 1 sec 2.1, the engine must reconcile broker reports
against its own disposal ledger and propose Form 8949 column (f)
adjustment codes for any disagreement.

This module is the parser side: it reads a broker CSV, applies a
caller-provided column-name mapping (so different brokers can be added
without code edits), and inserts one ``tax_1099da_lines`` row per
disposition.  The reconciler lives in ``tax/reconcile_1099da.py``.

PDF support is intentionally deferred to a manual session.  The
expectation is that the taxpayer will export the broker portal CSV
(every major exchange currently exposes one) and feed it here.

Mapping format (YAML or dict)::

    broker_id: coinbase
    form_year: 2025
    columns:
      payee_id:       account_id
      proceeds_usd:   gross_proceeds
      basis_usd:      cost_basis
      acquisition_date: date_acquired
      disposed_at:    date_sold
      symbol:         asset
      quantity:       qty
      wash_sale_loss_disallowed: wash_disallowed
      covered_status: covered_flag
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2.extras

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = (
    "payee_id", "proceeds_usd", "disposed_at", "symbol", "quantity",
)
OPTIONAL_COLUMNS = (
    "basis_usd", "acquisition_date", "wash_sale_loss_disallowed",
    "covered_status",
)
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

VALID_COVERED_STATUS = frozenset(("covered", "noncovered", "unknown"))


def _parse_date(s):
    if s is None or s == "":
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable acquisition_date: {s!r}")


def _parse_dt(s):
    if s is None or s == "":
        raise ValueError("disposed_at is required and was empty")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable disposed_at: {s!r}")


def _normalise_covered(value):
    if value is None or value == "":
        return "unknown"
    v = value.strip().lower()
    if v in ("covered", "y", "yes", "true", "1"):
        return "covered"
    if v in ("noncovered", "non-covered", "n", "no", "false", "0"):
        return "noncovered"
    if v in VALID_COVERED_STATUS:
        return v
    return "unknown"


def parse_csv(path, mapping):
    """Read ``path`` and return a list of normalised line dicts.

    The returned dicts use canonical engine column names regardless of
    the broker's source headers.  No DB access here -- pure parser.
    """
    cols = mapping.get("columns") or {}
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise ValueError(
            f"mapping[columns] missing required keys: {missing}"
        )

    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_idx, row in enumerate(reader, start=2):
            try:
                line = {
                    "payee_id": (row[cols["payee_id"]] or "").strip(),
                    "proceeds_usd": Decimal(
                        str(row[cols["proceeds_usd"]]).replace(",", "")
                        or "0"
                    ),
                    "disposed_at": _parse_dt(row[cols["disposed_at"]]),
                    "symbol": (row[cols["symbol"]] or "").strip().upper(),
                    "quantity": Decimal(
                        str(row[cols["quantity"]]).replace(",", "")
                    ),
                }

                if ("basis_usd" in cols
                        and row.get(cols["basis_usd"], "") != ""):
                    line["basis_usd"] = Decimal(
                        str(row[cols["basis_usd"]]).replace(",", "")
                    )
                else:
                    line["basis_usd"] = None

                if "acquisition_date" in cols:
                    line["acquisition_date"] = _parse_date(
                        row.get(cols["acquisition_date"], "")
                    )
                else:
                    line["acquisition_date"] = None

                if ("wash_sale_loss_disallowed" in cols and
                        row.get(
                            cols["wash_sale_loss_disallowed"], "") != ""):
                    line["wash_sale_loss_disallowed"] = Decimal(
                        str(row[cols["wash_sale_loss_disallowed"]]).replace(
                            ",", "")
                    )
                else:
                    line["wash_sale_loss_disallowed"] = None

                if "covered_status" in cols:
                    line["covered_status"] = _normalise_covered(
                        row.get(cols["covered_status"], "")
                    )
                else:
                    line["covered_status"] = "unknown"

                line["raw_row"] = {k: v for k, v in row.items()}
                out.append(line)
            except Exception as e:
                raise ValueError(
                    f"row {row_idx} of {path}: {e}"
                ) from e
    return out


def ingest_csv(conn, path, mapping, *, account_id=None):
    """Parse and insert.  Returns the number of new rows inserted.

    Duplicate rows (same UNIQUE key) are skipped via ON CONFLICT.
    """
    broker_id = mapping.get("broker_id")
    form_year = mapping.get("form_year")
    if not broker_id or not form_year:
        raise ValueError("mapping must include broker_id and form_year")

    lines = parse_csv(path, mapping)
    inserted = 0

    cur = conn.cursor()
    try:
        for line in lines:
            cur.execute(
                """
                INSERT INTO tax_1099da_lines
                    (broker_id, form_year, payee_id, proceeds_usd,
                     basis_usd, acquisition_date, disposed_at, symbol,
                     quantity, wash_sale_loss_disallowed,
                     covered_status, account_id, raw_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb)
                ON CONFLICT (broker_id, form_year, payee_id, symbol,
                             disposed_at, quantity) DO NOTHING
                RETURNING id
                """,
                (
                    broker_id, form_year, line["payee_id"],
                    line["proceeds_usd"], line["basis_usd"],
                    line["acquisition_date"], line["disposed_at"],
                    line["symbol"], line["quantity"],
                    line["wash_sale_loss_disallowed"],
                    line["covered_status"], account_id,
                    json.dumps(line["raw_row"], default=str),
                ),
            )
            if cur.fetchone() is not None:
                inserted += 1
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def list_lines(conn, *, broker_id=None, form_year=None):
    """Return rows for a broker / year, ordered chronologically."""
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
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
