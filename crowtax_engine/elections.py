"""Rev. Proc. 2024-28 allocation / method-election bookkeeping.

Roadmap item 1.8.  Rev. Proc. 2024-28 required taxpayers to make a
per-wallet basis allocation of all unused (pre-2025) crypto basis
*before* the first 2025 disposition.  Three permissible election
types:

    * ``specific_unit``   - Specific-Unit Allocation (SUA): caller
                            provides a lot -> account map.
    * ``global_alloc``    - Global allocation: a default ordering rule
                            (FIFO across all accounts) for all pre-2025
                            lots.
    * ``none_filed``      - No election was made before the deadline.
                            This is an open legal issue that the CPA
                            must be aware of.  When a report includes
                            post-2024 disposals and the election table
                            is empty or marked ``none_filed``, the
                            engine emits a prominent warning - never
                            silently defaults.

The ``tax_method_elections`` table is shared with item 1.1 (it is
created in migration 003).  This module adds the import / query / warn
surface.

Usage patterns::

    # One-off after the taxpayer provides an SUA document:
    elections.import_sua(conn,
        effective_date="2025-01-01",
        lot_account_map=[{"lot_id": 123, "account_id": 4}, ...],
        documentation_path="/path/to/signed_sua.pdf",
    )

    # Called by ``build_package()`` in item 1.7 before emission:
    warnings = elections.validate_for_year(conn, year=2026)
    if warnings:
        pkg.manifest["warnings"].extend(warnings)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Iterable, Optional

import psycopg2.extras

log = logging.getLogger(__name__)

VALID_ELECTION_TYPES = frozenset((
    "specific_unit",       # SUA: lot-by-lot mapping
    "global_alloc",        # Global: ordering rule
    "universal_pre_2025",  # Read-only flag for pre-2025 reports
    "none_filed",          # No Rev. Proc. 2024-28 election was made
))


def _date_to_iso(d) -> str:
    if isinstance(d, str):
        return d
    if isinstance(d, date):
        return d.isoformat()
    raise TypeError(f"cannot coerce {d!r} to ISO date")


def record_election(
    conn,
    *,
    effective_date,
    election_type: str,
    details: Optional[dict] = None,
    documentation_path: Optional[str] = None,
    locked: bool = True,
    notes: Optional[str] = None,
) -> int:
    """Insert a ``tax_method_elections`` row and return the id.

    When ``election_type`` is ``'none_filed'`` and ``locked=True`` the
    row is a permanent marker that the taxpayer did not file - never
    silently overwrite an existing none_filed marker.
    """
    if election_type not in VALID_ELECTION_TYPES:
        raise ValueError(
            f"election_type {election_type!r} not in "
            f"{sorted(VALID_ELECTION_TYPES)}"
        )
    details_json = json.dumps(details or {})

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_method_elections
                (effective_date, election_type, details_json,
                 documentation_path, locked, notes)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id
            """,
            (_date_to_iso(effective_date), election_type, details_json,
             documentation_path, locked, notes),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def import_sua(
    conn,
    *,
    effective_date,
    lot_account_map: Iterable[dict],
    documentation_path: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Import a Specific-Unit Allocation mapping.

    ``lot_account_map`` is an iterable of ``{"lot_id": int,
    "account_id": int}`` dicts.  The mapping is written into the
    election row's ``details_json`` and applied immediately to
    ``tax_lots.account_id`` for each listed lot.  Lots not appearing in
    the mapping are left untouched.

    Returns the new election row id.
    """
    mapping = list(lot_account_map)
    row_id = record_election(
        conn,
        effective_date=effective_date,
        election_type="specific_unit",
        details={"lot_account_map": mapping},
        documentation_path=documentation_path,
        notes=notes,
        locked=True,
    )
    cur = conn.cursor()
    try:
        for m in mapping:
            cur.execute(
                """
                UPDATE tax_lots
                SET account_id = %s
                WHERE id = %s
                """,
                (m["account_id"], m["lot_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return row_id


def record_none_filed(
    conn,
    *,
    effective_date,
    notes: Optional[str] = None,
) -> int:
    """Explicitly record that NO Rev. Proc. 2024-28 election was filed.

    Use this when the CPA confirms the taxpayer did not allocate before
    the first 2025 disposition.  Keeps the warning path active but
    makes the intent explicit in the audit trail.
    """
    return record_election(
        conn,
        effective_date=effective_date,
        election_type="none_filed",
        notes=notes or ("Rev. Proc. 2024-28 allocation not filed before "
                        "first 2025 disposition"),
        locked=True,
    )


def current_election(conn) -> Optional[dict]:
    """Return the most recent election row, or None if none exist."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT * FROM tax_method_elections
            ORDER BY effective_date DESC, id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close()


def _year_has_post_2024_disposals(conn, year: int) -> bool:
    """Return True iff the given report year contains any disposal whose
    ``disposed_at`` falls on or after 2025-01-01."""
    if year < 2025:
        return False
    cur = conn.cursor()
    try:
        year_start = int(
            datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
        )
        year_end = int(
            datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        )
        cur.execute(
            """
            SELECT 1 FROM tax_disposals
            WHERE disposed_at BETWEEN %s AND %s
            LIMIT 1
            """,
            (year_start, year_end),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()


def validate_for_year(conn, year: int) -> list[str]:
    """Return a list of human-readable warnings for the report year.

    Emits a warning when the report spans post-2024 dispositions AND
    either:
        * no election row exists, or
        * the most recent election row is ``election_type='none_filed'``.

    Callers (e.g. ``filing_package.build_package``) should surface these
    in the manifest + summary.json so the CPA cannot miss them.
    """
    if not _year_has_post_2024_disposals(conn, year):
        return []

    election = current_election(conn)
    warnings: list[str] = []

    if election is None:
        warnings.append(
            "Rev. Proc. 2024-28 election status is UNKNOWN: no row in "
            "tax_method_elections.  Filing year %d includes post-2024 "
            "dispositions - if no allocation was made before the first "
            "2025 sale, the return may be non-conformant.  Resolve "
            "with CPA before filing." % year
        )
        return warnings

    if election["election_type"] == "none_filed":
        warnings.append(
            "Rev. Proc. 2024-28 election = 'none_filed' (effective %s).  "
            "Filing year %d includes post-2024 dispositions; the lack "
            "of a timely allocation is an open legal issue that the "
            "CPA must address before filing." %
            (election["effective_date"], year)
        )

    return warnings


def election_status_summary(conn, year: int) -> dict:
    """Return a dict suitable for embedding in summary.json / manifest.

    Includes the human-readable election name, effective date, and the
    list of warnings from ``validate_for_year``.
    """
    election = current_election(conn)
    return {
        "rev_proc_2024_28_election": (
            election["election_type"] if election else "unknown"
        ),
        "effective_date": (
            str(election["effective_date"]) if election else None
        ),
        "documentation_path": (
            election.get("documentation_path") if election else None
        ),
        "warnings": validate_for_year(conn, year),
    }
