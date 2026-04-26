-- Tax Engine Migration 010 - 1099-DA broker reporting ingest
-- Run:  psql -d ponyboy -f tax/migrations_010_1099da.sql
--
-- Closes roadmap item 2.1.  IRS Form 1099-DA (final regs Treas. Decn
-- 10000, July 2024) requires custodial digital-asset brokers to report
-- gross proceeds beginning with the 2025 tax year and adjusted basis
-- beginning 2026.  This table holds one row per reported disposition,
-- preserving the verbatim broker submission so the reconciler can
-- compare against engine-computed disposals and propose Form 8949
-- column (f) adjustment codes.
--
-- Schema-flexible: the parser writes a column-name mapping into
-- raw_data so per-broker quirks (Coinbase / Kraken / Gemini) are
-- captured without code edits.  Additive only.

CREATE TABLE IF NOT EXISTS tax_1099da_lines (
    id SERIAL PRIMARY KEY,
    broker_id TEXT NOT NULL,
    form_year INT NOT NULL,
    payee_id TEXT NOT NULL,
    proceeds_usd NUMERIC(20, 6) NOT NULL,
    basis_usd NUMERIC(20, 6),
    acquisition_date DATE,
    disposed_at TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    quantity NUMERIC(30, 18) NOT NULL,
    wash_sale_loss_disallowed NUMERIC(20, 6),
    covered_status TEXT NOT NULL DEFAULT 'unknown',
    account_id INT REFERENCES tax_accounts(id),
    raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (broker_id, form_year, payee_id, symbol, disposed_at, quantity)
);

ALTER TABLE tax_1099da_lines
    DROP CONSTRAINT IF EXISTS chk_tax_1099da_covered_status;

ALTER TABLE tax_1099da_lines
    ADD CONSTRAINT chk_tax_1099da_covered_status
    CHECK (covered_status IN ('covered', 'noncovered', 'unknown'));

CREATE INDEX IF NOT EXISTS idx_tax_1099da_broker_year
    ON tax_1099da_lines(broker_id, form_year);

CREATE INDEX IF NOT EXISTS idx_tax_1099da_match_keys
    ON tax_1099da_lines(symbol, disposed_at, quantity);
