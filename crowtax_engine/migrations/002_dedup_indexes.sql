-- Tax Engine Migration 002 — dedup indexes and wash sale basis adjustment
-- Run after migrations.sql:  psql -d ponyboy -f tax/migrations_002_dedup_indexes.sql

-- Partial unique indexes backing source_tx_id dedup checks in promote_confirmed.
-- Without these indexes, SELECT … WHERE source_tx_id = %s is a sequential scan
-- and the "dedup" logic is best-effort (race-prone on concurrent ingestion).
CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_lots_source_tx_id
    ON tax_lots(source_tx_id)
    WHERE source_tx_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_disposals_source_tx_id
    ON tax_disposals(source_tx_id)
    WHERE source_tx_id IS NOT NULL;

-- Broaden the raw-transaction dedup to cover rows that carry a source_tx_id
-- inside raw_json but no on-chain tx_hash (executor fills, CSV imports).
-- The existing idx_tax_raw_dedup only protects rows with a non-null tx_hash.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_raw_source_tx_id
    ON tax_raw_transactions(source, (raw_json->>'source_tx_id'))
    WHERE raw_json ? 'source_tx_id';

-- Wash sale cost basis adjustment (IRS Section 1091)
-- Tracks the per-lot adjustment separately so rematch_all can reset state
-- idempotently without losing the original basis.
ALTER TABLE tax_lots
    ADD COLUMN IF NOT EXISTS wash_sale_basis_adjustment NUMERIC(20, 6) NOT NULL DEFAULT 0;

-- Per-disposal disallowed-loss total (for Form 8949 adjustment_amount).
ALTER TABLE tax_disposals
    ADD COLUMN IF NOT EXISTS wash_sale_disallowed_loss NUMERIC(20, 6) NOT NULL DEFAULT 0;
