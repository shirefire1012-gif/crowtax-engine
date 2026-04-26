-- Tax Engine Migration 003 — per-wallet / per-account basis tracking
-- Run:  psql -d ponyboy -f tax/migrations_003_per_wallet.sql
--
-- Closes roadmap item 1.1.  Implements the Treas. Reg. §1.1012-1(j) /
-- Rev. Proc. 2024-28 mandate that 2025-and-later dispositions match
-- basis per wallet / per account rather than across a universal pool.
--
-- Changes are additive: new ``tax_accounts`` and ``tax_method_elections``
-- tables, plus a nullable ``account_id`` FK on ``tax_lots`` and
-- ``tax_disposals``.  Existing rows keep NULL ``account_id`` until the
-- separate backfill script (``backfill_003_accounts.py``) runs.
--
-- The engine reads ``account_id`` when it is populated and falls back to
-- the prior universal-pool behaviour when it is NULL — so running this
-- migration alone does not change any 2024 or earlier report output.

-- 1. Canonical account identity: (source, wallet_address, chain).
--    For centralized exchanges, wallet_address is the exchange account
--    handle (or a synthetic placeholder like "hyperliquid:default");
--    for on-chain, it is the address.  Chain is the string we already
--    use on tax_lots.chain.
CREATE TABLE IF NOT EXISTS tax_accounts (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,              -- 'coinbase', 'binance', 'hyperliquid', 'onchain', ...
    wallet_address TEXT NOT NULL,      -- address for on-chain; handle for CEX (never NULL)
    chain TEXT NOT NULL,               -- 'BTC', 'ETH', 'SOL', 'HYPE', 'SUI', 'INK', 'none'
    display_name TEXT,                 -- human-readable label for reports
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source, wallet_address, chain)
);

CREATE INDEX IF NOT EXISTS idx_tax_accounts_source
    ON tax_accounts(source);

-- 2. Rev. Proc. 2024-28 allocation / method elections.
CREATE TABLE IF NOT EXISTS tax_method_elections (
    id SERIAL PRIMARY KEY,
    effective_date DATE NOT NULL,
    election_type TEXT NOT NULL,       -- 'universal_pre_2025', 'specific_unit', 'global_alloc', 'none_filed'
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    documentation_path TEXT,           -- local file path to the election doc (PDF, signed CSV, etc.)
    locked BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_method_elections_effective
    ON tax_method_elections(effective_date);

-- 3. Wire lots and disposals to accounts.  Nullable so historical rows
--    can be backfilled post-deploy without blocking the migration.
ALTER TABLE tax_lots
    ADD COLUMN IF NOT EXISTS account_id INT REFERENCES tax_accounts(id);

ALTER TABLE tax_disposals
    ADD COLUMN IF NOT EXISTS account_id INT REFERENCES tax_accounts(id);

-- 4. Compound indexes for the per-wallet lot selection SQL.
CREATE INDEX IF NOT EXISTS idx_tax_lots_account_symbol
    ON tax_lots(account_id, symbol, acquired_at)
    WHERE remaining_quantity > 0;

CREATE INDEX IF NOT EXISTS idx_tax_disposals_account_symbol
    ON tax_disposals(account_id, symbol, disposed_at);
