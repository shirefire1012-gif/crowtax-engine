"""Tax report generation: Form 8949 output in JSON, CSV, and text."""

import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import psycopg2.extras

from crowtax_engine.db import PONYBOY_DSN, get_conn
from crowtax_engine.engine import rematch_all
from crowtax_engine.models import Form8949Line, GainLossSummary, TaxReport

log = logging.getLogger(__name__)


def _epoch_to_datestr(epoch_seconds: int) -> str:
    """Convert epoch seconds to date string YYYY-MM-DD."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def generate_report(conn, year: int, method: str = "fifo",
                    suppress_zero_swaps: Optional[Decimal] = None) -> TaxReport:
    """Generate a tax report for the given year and cost basis method.

    Re-matches all disposals if no matches exist for the given method. The
    caller is responsible for the connection's autocommit / isolation mode;
    ``rematch_all`` commits internally.

    ``suppress_zero_swaps`` (roadmap 2.2): when set to a positive Decimal
    tolerance (e.g. ``Decimal('0.01')``), Form 8949 lines whose disposal
    is a wrap/stable same-family swap AND whose absolute gain/loss is at
    or below the tolerance are excluded from ``short_term_items`` /
    ``long_term_items``.  Schedule D totals are NOT changed - the
    aggregate proceeds/basis/gain still include the suppressed lines.
    Default ``None`` = no suppression.  This is opt-in only with CPA
    sign-off; a depegged stable swap (e.g. USDC at $0.95) produces a
    real loss that exceeds the tolerance and is therefore retained.
    """
    # Check if matches exist for this method
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM tax_lot_matches WHERE method = %s", (method,))
    match_count = cur.fetchone()[0]
    cur.close()

    if match_count == 0:
        log.info("No matches found for method=%s, running rematch_all", method)
        rematch_all(conn, method)

    # Year boundaries in epoch seconds
    year_start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    year_end = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT m.*, d.disposed_at, d.symbol, d.wash_sale_flag,
                   d.quantity as disposal_qty,
                   d.wash_sale_disallowed_loss as disposal_disallowed_loss,
                   d.wrap_family as disposal_wrap_family,
                   l.acquired_at, l.symbol as lot_symbol,
                   l.asset_class as lot_asset_class
            FROM tax_lot_matches m
            JOIN tax_disposals d ON m.disposal_id = d.id
            JOIN tax_lots l ON m.lot_id = l.id
            WHERE d.disposed_at BETWEEN %s AND %s
              AND m.method = %s
            ORDER BY d.disposed_at ASC, m.id ASC
        """, (year_start, year_end, method))

        rows = cur.fetchall()
    finally:
        cur.close()

    # Disposal-level disallowed loss needs to be split across each match row
    # for this disposal, pro-rata by matched quantity, so every Form8949 line
    # carries its share of the adjustment. Cents-level residual is pushed
    # onto the last line to preserve the total exactly.
    by_disposal_qty_total = {}
    by_disposal_disallowed = {}
    disposal_last_match_id = {}
    for row in rows:
        did = row["disposal_id"]
        by_disposal_qty_total[did] = (
            by_disposal_qty_total.get(did, Decimal(0))
            + Decimal(str(row["quantity_matched"]))
        )
        by_disposal_disallowed[did] = Decimal(
            str(row["disposal_disallowed_loss"] or 0)
        )
        # Track the ID of the last row seen for each disposal; rows are
        # already ordered by m.id ASC within each disposal.
        disposal_last_match_id[did] = row["id"]

    # Track allocated adjustment per disposal so we can put the remainder on
    # the last row of the disposal.
    disposal_allocated = {did: Decimal(0) for did in by_disposal_qty_total}

    short_items = []
    long_items = []
    short_total = GainLossSummary()
    long_total = GainLossSummary()
    wash_count = 0
    seen_disposals = set()

    for row in rows:
        qty = float(row["quantity_matched"])
        symbol = row["symbol"]
        proceeds = float(row["proceeds_usd"])
        cost_basis = float(row["cost_basis_usd"])
        gain_loss = float(row["gain_loss_usd"])
        wash = bool(row["wash_sale_flag"])

        did = row["disposal_id"]
        total_disallowed = by_disposal_disallowed[did]
        qty_total = by_disposal_qty_total[did]
        if wash and total_disallowed > 0 and qty_total > 0:
            if row["id"] == disposal_last_match_id[did]:
                # Absorb any rounding residual on the last row of the disposal.
                per_line_adj = total_disallowed - disposal_allocated[did]
            else:
                per_line_adj = (
                    total_disallowed * Decimal(str(row["quantity_matched"]))
                    / qty_total
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                disposal_allocated[did] += per_line_adj
            adjustment_amount = float(per_line_adj)
        else:
            adjustment_amount = 0.0

        wrap_family = row.get("disposal_wrap_family")
        asset_class = row.get("lot_asset_class") or "fungible"

        line = Form8949Line(
            description=f"{qty:.8g} {symbol}",
            date_acquired=_epoch_to_datestr(row["acquired_at"]),
            date_sold=_epoch_to_datestr(row["disposed_at"]),
            proceeds=round(proceeds, 2),
            cost_basis=round(cost_basis, 2),
            gain_loss=round(gain_loss, 2),
            wash_sale=wash,
            adjustment_code="W" if wash and total_disallowed > 0 else "",
            adjustment_amount=round(adjustment_amount, 2),
            box="C",
            wrap_family=wrap_family,
            asset_class=asset_class,
        )

        if wash and did not in seen_disposals:
            wash_count += 1
            seen_disposals.add(did)

        # Roadmap 2.2 — opt-in suppression of near-zero wrap/stable
        # swap LINES.  Schedule D totals must still include them, so we
        # update the totals unconditionally and only skip the per-line
        # append when the threshold is met.
        suppressed = (
            suppress_zero_swaps is not None
            and wrap_family is not None
            and abs(Decimal(str(gain_loss))) <= suppress_zero_swaps
        )

        if row["holding_period"] == "short":
            if not suppressed:
                short_items.append(line)
            short_total.total_proceeds += proceeds
            short_total.total_cost_basis += cost_basis
            short_total.total_gain_loss += gain_loss
            short_total.num_transactions += 1
            if wash:
                short_total.wash_sale_count += 1
        else:
            if not suppressed:
                long_items.append(line)
            long_total.total_proceeds += proceeds
            long_total.total_cost_basis += cost_basis
            long_total.total_gain_loss += gain_loss
            long_total.num_transactions += 1
            if wash:
                long_total.wash_sale_count += 1

    # Round totals
    for t in (short_total, long_total):
        t.total_proceeds = round(t.total_proceeds, 2)
        t.total_cost_basis = round(t.total_cost_basis, 2)
        t.total_gain_loss = round(t.total_gain_loss, 2)

    return TaxReport(
        year=year,
        method=method,
        short_term_items=short_items,
        long_term_items=long_items,
        short_term_total=short_total,
        long_term_total=long_total,
        wash_sale_count=wash_count,
    )


def export_csv(report: TaxReport, filepath: str):
    """Export report to Form 8949 CSV format."""
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Description", "Date Acquired", "Date Sold",
            "Proceeds", "Cost Basis", "Adjustment Code",
            "Adjustment Amount", "Gain or Loss", "Box",
            "Holding Period",
        ])

        for item in report.short_term_items:
            writer.writerow([
                item.description, item.date_acquired, item.date_sold,
                f"{item.proceeds:.2f}", f"{item.cost_basis:.2f}",
                item.adjustment_code, f"{item.adjustment_amount:.2f}",
                f"{item.gain_loss:.2f}", item.box, "Short-term",
            ])

        for item in report.long_term_items:
            writer.writerow([
                item.description, item.date_acquired, item.date_sold,
                f"{item.proceeds:.2f}", f"{item.cost_basis:.2f}",
                item.adjustment_code, f"{item.adjustment_amount:.2f}",
                f"{item.gain_loss:.2f}", item.box, "Long-term",
            ])


def export_json(report: TaxReport) -> dict:
    """Export report as a dictionary."""
    def line_to_dict(line: Form8949Line) -> dict:
        return {
            "description": line.description,
            "date_acquired": line.date_acquired,
            "date_sold": line.date_sold,
            "proceeds": line.proceeds,
            "cost_basis": line.cost_basis,
            "gain_loss": line.gain_loss,
            "wash_sale": line.wash_sale,
            "adjustment_code": line.adjustment_code,
            "adjustment_amount": line.adjustment_amount,
            "box": line.box,
            "wrap_family": line.wrap_family,
            "asset_class": line.asset_class,
        }

    def summary_to_dict(s: GainLossSummary) -> dict:
        return {
            "total_proceeds": s.total_proceeds,
            "total_cost_basis": s.total_cost_basis,
            "total_gain_loss": s.total_gain_loss,
            "num_transactions": s.num_transactions,
            "wash_sale_count": s.wash_sale_count,
        }

    return {
        "year": report.year,
        "method": report.method,
        "short_term": {
            "items": [line_to_dict(i) for i in report.short_term_items],
            "totals": summary_to_dict(report.short_term_total),
        },
        "long_term": {
            "items": [line_to_dict(i) for i in report.long_term_items],
            "totals": summary_to_dict(report.long_term_total),
        },
        "wash_sale_count": report.wash_sale_count,
    }


def export_text(report: TaxReport) -> str:
    """Export report as a human-readable text summary."""
    lines = []
    lines.append(f"Tax Report — {report.year} (Method: {report.method.upper()})")
    lines.append("=" * 60)

    def _section(title, items, totals):
        lines.append(f"\n{title}")
        lines.append("-" * 60)
        if not items:
            lines.append("  No transactions")
            return
        for item in items:
            wash_marker = " [WASH SALE]" if item.wash_sale else ""
            lines.append(
                f"  {item.description}  "
                f"Acquired: {item.date_acquired}  Sold: {item.date_sold}  "
                f"Proceeds: ${item.proceeds:,.2f}  Basis: ${item.cost_basis:,.2f}  "
                f"Gain/Loss: ${item.gain_loss:,.2f}{wash_marker}")
        lines.append(f"\n  Totals: {totals.num_transactions} transactions")
        lines.append(f"  Proceeds:   ${totals.total_proceeds:,.2f}")
        lines.append(f"  Cost Basis: ${totals.total_cost_basis:,.2f}")
        lines.append(f"  Gain/Loss:  ${totals.total_gain_loss:,.2f}")
        if totals.wash_sale_count:
            lines.append(f"  Wash Sales: {totals.wash_sale_count}")

    _section("Part I — Short-Term Capital Gains and Losses",
             report.short_term_items, report.short_term_total)
    _section("Part II — Long-Term Capital Gains and Losses",
             report.long_term_items, report.long_term_total)

    combined_gain = (report.short_term_total.total_gain_loss +
                     report.long_term_total.total_gain_loss)
    lines.append(f"\n{'=' * 60}")
    lines.append(f"Combined Net Gain/Loss: ${combined_gain:,.2f}")
    if report.wash_sale_count:
        lines.append(f"Total Wash Sales Flagged: {report.wash_sale_count}")

    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Generate tax report")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--method", default="fifo",
                        choices=["fifo", "lifo", "hifo", "specific_id"])
    parser.add_argument("--format", default="text",
                        choices=["csv", "json", "text"])
    parser.add_argument("--output", help="Output file path (default: stdout)")
    parser.add_argument(
        "--suppress-zero-swaps",
        type=Decimal,
        default=None,
        help="Roadmap 2.2: hide wrap/stable swap LINES whose absolute "
             "gain/loss is at or below this USD tolerance (e.g. 0.01). "
             "Schedule D totals are unaffected.  CPA sign-off required.",
    )
    args = parser.parse_args()

    conn = get_conn(PONYBOY_DSN)
    conn.autocommit = False
    try:
        report = generate_report(
            conn, args.year, args.method,
            suppress_zero_swaps=args.suppress_zero_swaps,
        )

        if args.format == "csv":
            if not args.output:
                args.output = f"tax_{args.year}_{args.method}.csv"
            export_csv(report, args.output)
            print(f"CSV report written to {args.output}")
        elif args.format == "json":
            output = json.dumps(export_json(report), indent=2)
            if args.output:
                with open(args.output, "w") as f:
                    f.write(output)
                print(f"JSON report written to {args.output}")
            else:
                print(output)
        else:
            output = export_text(report)
            if args.output:
                with open(args.output, "w") as f:
                    f.write(output)
                print(f"Text report written to {args.output}")
            else:
                print(output)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
