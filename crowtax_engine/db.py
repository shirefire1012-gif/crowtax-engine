"""Minimal Postgres connection helper for the crowtax engine.

The engine is database-backed (Postgres). This module provides:
- ``CROWTAX_ENGINE_DATABASE_URL`` / ``PONYBOY_DSN`` env-var resolution
- ``get_conn(dsn=None)`` returning a fresh ``psycopg2`` connection

Callers typically own connection lifecycle (open, set autocommit, run, commit, close).
"""

from __future__ import annotations

import os

import psycopg2

# Backward-compat: legacy env var name from The Crow Show platform.
PONYBOY_DSN: str = os.environ.get(
    "CROWTAX_ENGINE_DATABASE_URL",
    os.environ.get(
        "PONYBOY_DSN",
        "postgresql://localhost/crowtax_engine",
    ),
)


def get_conn(dsn: str | None = None):
    """Return a fresh psycopg2 connection. Caller closes it."""
    return psycopg2.connect(dsn or PONYBOY_DSN)
