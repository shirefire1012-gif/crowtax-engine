"""Tax engine test fixtures.

Uses a dedicated Postgres test database (default ``crowtax_engine_test``)
for all tax-engine tests. Configure via ``CROWTAX_ENGINE_TEST_DATABASE_URL``
(or ``TAX_TEST_DSN`` for backward compat). The schema is rebuilt from
``crowtax_engine/migrations/*.sql`` once per session; each test function
starts with a truncated schema so nothing leaks between tests.

The engine and staging modules call ``conn.commit()`` internally, so a
rolling-transaction fixture is not sufficient. TRUNCATE wipes rows while
preserving DDL.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "crowtax_engine" / "migrations"

TEST_DSN = os.environ.get(
    "CROWTAX_ENGINE_TEST_DATABASE_URL",
    os.environ.get("TAX_TEST_DSN", "dbname=crowtax_engine_test"),
)


def _apply_migrations(conn) -> None:
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        raise RuntimeError(
            f"No migrations found under {MIGRATIONS_DIR}. "
            "Repo layout may be wrong."
        )
    cur = conn.cursor()
    try:
        for f in sql_files:
            cur.execute(f.read_text())
        conn.commit()
    finally:
        cur.close()


def _truncate_tax_tables(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tablename LIKE 'tax_%'
            """
        )
        tables = [r[0] for r in cur.fetchall()]
        if tables:
            cur.execute(
                f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"
            )
        conn.commit()
    finally:
        cur.close()


@pytest.fixture(scope="session")
def _migrated_dsn():
    conn = psycopg2.connect(TEST_DSN)
    try:
        _apply_migrations(conn)
    finally:
        conn.close()
    return TEST_DSN


@pytest.fixture
def db(_migrated_dsn):
    conn = psycopg2.connect(_migrated_dsn)
    conn.autocommit = False
    try:
        _truncate_tax_tables(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()
