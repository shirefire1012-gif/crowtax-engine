"""Structured filing-package output - Form 8949 + Schedule D + Schedule 1.

Roadmap item 1.7.  ``tax/report.py`` historically emitted a single
Form-8949-flavoured CSV.  A filing-ready package needs to separate:

    * Form 8949 Part I  (short-term capital gain / loss).
    * Form 8949 Part II (long-term capital gain / loss).
    * Schedule D summary (totals that reconcile to 8949 line sums).
    * Schedule 1 ordinary-income (mining, staking, airdrop, fork,
      funding received / paid).
    * NC D-400 AGI contribution (single-line JSON; file 1 section 3
      notes no NC-specific adjustments).
    * summary.json  (top-level totals).
    * manifest.json (source-file fingerprint so the CPA knows exactly
      what data fed this package).

This module produces ``FilingPackage`` from the existing
``tax.report.generate_report`` plus the ordinary-income /funding
aggregators from items 1.5 and 1.6, and writes the bundle to a
timestamped directory.  Reconciliation invariant: Schedule D totals
equal Part I + Part II sums (enforced in ``_build_schedule_d``).
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from crowtax_engine import elections, funding, ordinary_income

log = logging.getLogger(__name__)


@dataclass
class ScheduleDLine:
    description: str
    total_proceeds: Decimal
    total_cost_basis: Decimal
    adjustments: Decimal
    total_gain_loss: Decimal
    # Roadmap 2.4 - True only for the segregated 28% collectibles line.
    rate_28pct_collectibles: bool = False


@dataclass
class FilingPackage:
    year: int
    method: str
    form_8949_part_i: list[dict] = field(default_factory=list)   # short-term
    form_8949_part_ii: list[dict] = field(default_factory=list)  # long-term
    # Roadmap 2.4 - long-term NFT collectibles ride a separate Form 8949
    # so the 28% IRC sec 1(h)(4) rate can be applied independently.
    form_8949_collectibles: list[dict] = field(default_factory=list)
    schedule_d_summary: list[ScheduleDLine] = field(default_factory=list)
    schedule_1_ordinary_income_by_type: dict[str, Decimal] = field(
        default_factory=dict)
    funding_summary: dict[str, Decimal] = field(default_factory=dict)
    nc_d400_agi_contribution: Decimal = Decimal(0)
    manifest: dict[str, Any] = field(default_factory=dict)


def _line_to_dict(line) -> dict:
    """Convert a Form8949Line dataclass to a plain dict for CSV/JSON."""
    return {
        "description": line.description,
        "date_acquired": line.date_acquired,
        "date_sold": line.date_sold,
        "proceeds": line.proceeds,
        "cost_basis": line.cost_basis,
        "adjustment_code": line.adjustment_code,
        "adjustment_amount": line.adjustment_amount,
        "gain_loss": line.gain_loss,
        "wash_sale": line.wash_sale,
        "box": line.box,
        "wrap_family": getattr(line, "wrap_family", None),
        "asset_class": getattr(line, "asset_class", "fungible"),
    }


def _sum_lines(lines: list[dict]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    proceeds = sum((Decimal(str(l["proceeds"])) for l in lines), Decimal(0))
    basis = sum((Decimal(str(l["cost_basis"])) for l in lines), Decimal(0))
    adj = sum(
        (Decimal(str(l.get("adjustment_amount") or 0)) for l in lines),
        Decimal(0),
    )
    gl = sum((Decimal(str(l["gain_loss"])) for l in lines), Decimal(0))
    return proceeds, basis, adj, gl


def build_package(
    conn,
    year: int,
    method: str = "fifo",
    *,
    source_files: Optional[list[str]] = None,
) -> FilingPackage:
    """Assemble a ``FilingPackage`` for the given tax year.

    Calls ``tax.report.generate_report`` for the capital-gain side,
    then folds in Schedule 1 ordinary income (from items 1.5 and 1.6).
    """
    # Defer the import so this module does not pull in production DSN
    # config (``storage.pg_store_v2``) when tests exercise it against
    # the test DB.
    from crowtax_engine.report import generate_report

    report = generate_report(conn, year=year, method=method)

    part_i = [_line_to_dict(l) for l in report.short_term_items]
    part_ii_all = [_line_to_dict(l) for l in report.long_term_items]

    # Roadmap 2.4: long-term collectible-NFT lines ride a separate Form
    # 8949 + Schedule D line so the 28% IRC sec 1(h)(4) rate applies.
    # Short-term collectibles flow with ordinary capital-gain rates and
    # remain on Part I.  Non-collectible NFTs flow with generic crypto.
    part_ii = [
        l for l in part_ii_all
        if l.get("asset_class") != "nft_collectible"
    ]
    collectibles = [
        l for l in part_ii_all
        if l.get("asset_class") == "nft_collectible"
    ]

    short_proc, short_basis, short_adj, short_gl = _sum_lines(part_i)
    long_proc, long_basis, long_adj, long_gl = _sum_lines(part_ii)
    coll_proc, coll_basis, coll_adj, coll_gl = _sum_lines(collectibles)
    net_proc = short_proc + long_proc + coll_proc
    net_basis = short_basis + long_basis + coll_basis
    net_adj = short_adj + long_adj + coll_adj
    net_gl = short_gl + long_gl + coll_gl

    schedule_d = [
        ScheduleDLine(
            description="Short-term total (Form 8949 Part I)",
            total_proceeds=short_proc,
            total_cost_basis=short_basis,
            adjustments=short_adj,
            total_gain_loss=short_gl,
        ),
        ScheduleDLine(
            description="Long-term total (Form 8949 Part II)",
            total_proceeds=long_proc,
            total_cost_basis=long_basis,
            adjustments=long_adj,
            total_gain_loss=long_gl,
        ),
    ]
    if collectibles:
        schedule_d.append(ScheduleDLine(
            description=("Long-term collectibles (NFT) "
                         "- 28% rate per IRC sec 1(h)(4)"),
            total_proceeds=coll_proc,
            total_cost_basis=coll_basis,
            adjustments=coll_adj,
            total_gain_loss=coll_gl,
            rate_28pct_collectibles=True,
        ))
    schedule_d.append(ScheduleDLine(
        description="Net capital gain/loss",
        total_proceeds=net_proc,
        total_cost_basis=net_basis,
        adjustments=net_adj,
        total_gain_loss=net_gl,
    ))

    ordinary = ordinary_income.summarize_by_year_and_type(conn, year=year)
    # Normalise: ensure all four known types present even when zero.
    for t in ("mining", "staking", "airdrop", "fork"):
        ordinary.setdefault(t, Decimal(0))

    funding_totals = funding.summarize_by_year(conn, year=year)

    # NC D-400 contribution = federal AGI change from crypto
    # = short cap gain + long cap gain + ordinary (file 1 section 3,
    # no NC-specific adjustments).
    ordinary_total = sum(ordinary.values(), Decimal(0))
    nc_agi = net_gl + ordinary_total + funding_totals.get("net", Decimal(0))

    pkg = FilingPackage(
        year=year,
        method=method,
        form_8949_part_i=part_i,
        form_8949_part_ii=part_ii,
        form_8949_collectibles=collectibles,
        schedule_d_summary=schedule_d,
        schedule_1_ordinary_income_by_type=ordinary,
        funding_summary=funding_totals,
        nc_d400_agi_contribution=nc_agi,
    )
    election_status = elections.election_status_summary(conn, year)
    pkg.manifest = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "year": year,
        "method": method,
        "source_files": source_files or [],
        "engine_version": "phase1-compliance",
        "rev_proc_2024_28_election": election_status["rev_proc_2024_28_election"],
        "election_effective_date": election_status["effective_date"],
        "election_documentation_path": election_status["documentation_path"],
        "warnings": election_status["warnings"],
    }
    return pkg


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def _decimal_dict(d: dict) -> dict:
    """Render a dict of Decimals as strings for JSON serialisation."""
    return {k: str(v) for k, v in d.items()}


def export_package(pkg: FilingPackage, out_dir: str) -> dict:
    """Write the package files into ``out_dir`` (created if missing).

    Emits exactly these files (item 1.7 acceptance criterion):
        form_8949_part_i.csv
        form_8949_part_ii.csv
        schedule_d_summary.csv
        schedule_1_ordinary.csv
        nc_d400_agi.json
        summary.json
        manifest.json

    Returns a dict mapping file name -> absolute path.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    form_header = [
        "description", "date_acquired", "date_sold", "proceeds",
        "cost_basis", "adjustment_code", "adjustment_amount",
        "gain_loss", "wash_sale", "box", "wrap_family", "asset_class",
    ]
    _write_csv(out / "form_8949_part_i.csv", pkg.form_8949_part_i, form_header)
    _write_csv(out / "form_8949_part_ii.csv", pkg.form_8949_part_ii, form_header)
    # Roadmap 2.4 - separate Form 8949 for long-term collectibles (28%).
    _write_csv(
        out / "form_8949_collectibles.csv",
        pkg.form_8949_collectibles,
        form_header,
    )

    sd_rows = [
        {
            "description": sd.description,
            "total_proceeds": str(sd.total_proceeds),
            "total_cost_basis": str(sd.total_cost_basis),
            "adjustments": str(sd.adjustments),
            "total_gain_loss": str(sd.total_gain_loss),
            "rate_28pct_collectibles": "true" if sd.rate_28pct_collectibles
                                       else "false",
        }
        for sd in pkg.schedule_d_summary
    ]
    _write_csv(
        out / "schedule_d_summary.csv",
        sd_rows,
        ["description", "total_proceeds", "total_cost_basis",
         "adjustments", "total_gain_loss",
         "rate_28pct_collectibles"],
    )

    s1_rows = [
        {"income_type": k, "total_fmv_usd": str(v)}
        for k, v in pkg.schedule_1_ordinary_income_by_type.items()
    ]
    # Funding items are Schedule 1 line 8 as well; file them in the
    # same CSV for CPA convenience.
    for direction in ("received", "paid"):
        if direction in pkg.funding_summary:
            s1_rows.append({
                "income_type": f"perp_funding_{direction}",
                "total_fmv_usd": str(pkg.funding_summary[direction]),
            })
    _write_csv(
        out / "schedule_1_ordinary.csv",
        s1_rows,
        ["income_type", "total_fmv_usd"],
    )

    (out / "nc_d400_agi.json").write_text(json.dumps({
        "year": pkg.year,
        "federal_agi_contribution_usd": str(pkg.nc_d400_agi_contribution),
        "nc_specific_adjustments": [],
    }, indent=2))

    summary = {
        "year": pkg.year,
        "method": pkg.method,
        "form_8949_part_i_lines": len(pkg.form_8949_part_i),
        "form_8949_part_ii_lines": len(pkg.form_8949_part_ii),
        "form_8949_collectibles_lines": len(pkg.form_8949_collectibles),
        "schedule_d": [
            {
                "description": sd.description,
                "total_proceeds": str(sd.total_proceeds),
                "total_cost_basis": str(sd.total_cost_basis),
                "adjustments": str(sd.adjustments),
                "total_gain_loss": str(sd.total_gain_loss),
                "rate_28pct_collectibles": sd.rate_28pct_collectibles,
            }
            for sd in pkg.schedule_d_summary
        ],
        "schedule_1_ordinary_income_by_type": _decimal_dict(
            pkg.schedule_1_ordinary_income_by_type),
        "funding_summary": _decimal_dict(pkg.funding_summary),
        "nc_d400_agi_contribution_usd": str(pkg.nc_d400_agi_contribution),
        "rev_proc_2024_28_election": pkg.manifest.get(
            "rev_proc_2024_28_election", "unknown"),
        "warnings": pkg.manifest.get("warnings", []),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "manifest.json").write_text(json.dumps(pkg.manifest, indent=2))

    return {
        name: str((out / name).resolve())
        for name in (
            "form_8949_part_i.csv", "form_8949_part_ii.csv",
            "form_8949_collectibles.csv",
            "schedule_d_summary.csv", "schedule_1_ordinary.csv",
            "nc_d400_agi.json", "summary.json", "manifest.json",
        )
    }
