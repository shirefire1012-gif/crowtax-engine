"""Account (wallet / exchange-account) identity resolution.

Item 1.1 of the Phase 1 tax roadmap.  TD 10000 § 1.1012-1(h),(j) and
Rev. Proc. 2024-28 require per-wallet / per-account basis tracking for
digital-asset dispositions on or after 2025-01-01.

An "account" in this engine is a triple ``(source, wallet_address,
chain)``:

    * ``source``          — the origin system: ``coinbase``, ``binance``,
                            ``hyperliquid``, ``onchain``, ``executor``,
                            ``csv``, …
    * ``wallet_address``  — an on-chain address for custodial wallets,
                            the exchange-account handle for CEX rows,
                            or a stable synthetic placeholder (e.g.
                            ``hyperliquid:default``) when the ingest path
                            cannot expose the real handle yet.
    * ``chain``           — the chain or venue string already carried on
                            ``tax_lots.chain`` / ``tax_disposals.chain``.

The goal is a stable, human-readable primary key for basis-pool scope.
``get_or_create_account`` is idempotent by virtue of the
``UNIQUE (source, wallet_address, chain)`` index on ``tax_accounts``.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Fallback placeholder when an ingest path cannot expose a real wallet
# handle.  Never used for on-chain rows (they have real addresses).
DEFAULT_WALLET_PLACEHOLDER = "default"


def canonicalize(source: str, wallet_address: Optional[str],
                 chain: Optional[str]) -> tuple[str, str, str]:
    """Normalise the ``(source, wallet_address, chain)`` triple.

    * ``source`` is lower-cased and stripped.
    * ``wallet_address`` defaults to ``<source>:default`` when empty, so
      pre-1.1 rows that never carried a wallet still resolve to a single
      deterministic account per source.
    * ``chain`` defaults to ``"none"`` when empty.

    Hex addresses are lower-cased to dodge case-sensitivity mismatches
    between explorers.  Non-hex handles are left case-sensitive.
    """
    src = (source or "unknown").strip().lower()
    ch = (chain or "none").strip() or "none"

    wa = (wallet_address or "").strip()
    if not wa:
        wa = f"{src}:{DEFAULT_WALLET_PLACEHOLDER}"
    elif wa.startswith("0x") and len(wa) >= 40:
        wa = wa.lower()

    return src, wa, ch


def get_or_create_account(
    conn,
    source: str,
    wallet_address: Optional[str],
    chain: Optional[str],
    display_name: Optional[str] = None,
) -> int:
    """Resolve the triple to a ``tax_accounts.id``, inserting if needed.

    Commits on success.  The caller is responsible for the connection's
    autocommit / isolation mode.
    """
    src, wa, ch = canonicalize(source, wallet_address, chain)
    label = display_name or f"{src}:{wa}"

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tax_accounts
                (source, wallet_address, chain, display_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, wallet_address, chain) DO UPDATE
                SET display_name = COALESCE(
                    tax_accounts.display_name, EXCLUDED.display_name)
            RETURNING id
            """,
            (src, wa, ch, label),
        )
        account_id = cur.fetchone()[0]
        conn.commit()
        return account_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def get_account(conn, account_id: int) -> Optional[dict]:
    """Return the account row, or None if the id is not present."""
    import psycopg2.extras

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM tax_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close()


def list_accounts(conn) -> list[dict]:
    """Return every account row, ordered by source then wallet."""
    import psycopg2.extras

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            SELECT * FROM tax_accounts
            ORDER BY source, wallet_address, chain
            """
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
