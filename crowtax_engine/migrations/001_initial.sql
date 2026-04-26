-- Tax Engine Schema Migration
-- Run: psql -d ponyboy -f tax/migrations.sql

-- Permanent archive of all source data (raw_json is source of truth)
CREATE TABLE IF NOT EXISTS tax_raw_transactions (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,                    -- 'executor', 'dex', 'csv'
    source_file TEXT,                        -- filename for CSV imports
    chain TEXT,                              -- 'BTC', 'ETH', 'SOL', 'HYPE', 'SUI', 'INK'
    tx_hash TEXT,                            -- on-chain tx hash (null for executor/csv)
    block_number BIGINT,                     -- on-chain block (null for executor/csv)
    timestamp BIGINT NOT NULL,               -- epoch seconds
    raw_json JSONB NOT NULL,                 -- full original data
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, confirmed, promoted, rejected
    confirmation_count INT DEFAULT 0,
    required_confirmations INT DEFAULT 1,
    promoted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_raw_status ON tax_raw_transactions(status);
CREATE INDEX IF NOT EXISTS idx_tax_raw_source ON tax_raw_transactions(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_raw_dedup ON tax_raw_transactions(source, tx_hash) WHERE tx_hash IS NOT NULL;

-- Every acquisition of crypto
CREATE TABLE IF NOT EXISTS tax_lots (
    id SERIAL PRIMARY KEY,
    wallet_address TEXT,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    acquired_at BIGINT NOT NULL,             -- epoch seconds
    quantity NUMERIC(30, 18) NOT NULL,
    cost_basis_usd NUMERIC(20, 6) NOT NULL,
    cost_basis_per_unit NUMERIC(20, 6) NOT NULL,
    remaining_quantity NUMERIC(30, 18) NOT NULL,
    acquisition_type TEXT NOT NULL DEFAULT 'purchase',  -- purchase, swap, airdrop, fork, staking, mining, gift, other
    fee_usd NUMERIC(20, 6) DEFAULT 0,
    source TEXT NOT NULL,                    -- 'executor', 'dex', 'csv'
    source_tx_id TEXT,                       -- dedup key
    raw_transaction_id INT REFERENCES tax_raw_transactions(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_lots_symbol ON tax_lots(symbol, acquired_at);
CREATE INDEX IF NOT EXISTS idx_tax_lots_remaining ON tax_lots(symbol) WHERE remaining_quantity > 0;

-- Every sale/spend/swap
CREATE TABLE IF NOT EXISTS tax_disposals (
    id SERIAL PRIMARY KEY,
    wallet_address TEXT,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    disposed_at BIGINT NOT NULL,             -- epoch seconds
    quantity NUMERIC(30, 18) NOT NULL,
    proceeds_usd NUMERIC(20, 6) NOT NULL,
    fee_usd NUMERIC(20, 6) DEFAULT 0,
    source TEXT NOT NULL,
    source_tx_id TEXT,
    raw_transaction_id INT REFERENCES tax_raw_transactions(id),
    wash_sale_flag BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_disposals_symbol ON tax_disposals(symbol, disposed_at);

-- Which lots were consumed by which disposals
CREATE TABLE IF NOT EXISTS tax_lot_matches (
    id SERIAL PRIMARY KEY,
    disposal_id INT NOT NULL REFERENCES tax_disposals(id),
    lot_id INT NOT NULL REFERENCES tax_lots(id),
    quantity_matched NUMERIC(30, 18) NOT NULL,
    cost_basis_usd NUMERIC(20, 6) NOT NULL,
    proceeds_usd NUMERIC(20, 6) NOT NULL,
    gain_loss_usd NUMERIC(20, 6) NOT NULL,
    holding_period TEXT NOT NULL,            -- 'short' or 'long'
    method TEXT NOT NULL,                    -- 'fifo', 'lifo', 'hifo', 'specific_id'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_matches_disposal ON tax_lot_matches(disposal_id);

-- Track imported CSV files (prevent double-import)
CREATE TABLE IF NOT EXISTS tax_csv_imports (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    exchange TEXT NOT NULL,
    rows_imported INT NOT NULL,
    imported_at TIMESTAMPTZ DEFAULT NOW(),
    raw_json JSONB
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_csv_dedup ON tax_csv_imports(filename, exchange);
