-- Tax Engine Migration 006 - ordinary-income acquisition ledger
-- Run:  psql -d ponyboy -f tax/migrations_006_ordinary_income.sql
--
-- Closes roadmap item 1.5.  Mining (Notice 2014-21 Q-8), staking
-- (Rev. Rul. 2023-14), airdrops and hard forks (Rev. Rul. 2019-24)
-- produce ordinary income at FMV on receipt, with basis in the acquired
-- coins set to that same FMV.  The income flows through Schedule 1 and
-- is separate from Form 8949 capital gain reporting.
--
-- This table is the single source of truth for Schedule 1 line items
-- of type mining / staking / airdrop / fork.  Item 1.6 will add a
-- parallel ``tax_funding_events`` table for perp funding.

CREATE TABLE IF NOT EXISTS tax_ordinary_income (
    id SERIAL PRIMARY KEY,
    account_id INT REFERENCES tax_accounts(id),
    symbol TEXT NOT NULL,
    received_at BIGINT NOT NULL,           -- epoch seconds, date of receipt
    quantity NUMERIC(30, 18) NOT NULL,
    fmv_usd NUMERIC(20, 6) NOT NULL,       -- total FMV at receipt
    fmv_per_unit NUMERIC(20, 6) NOT NULL,  -- fmv_usd / quantity
    fmv_source TEXT,                       -- 'price_oracle', 'csv', 'stablecoin_par', 'manual'
    income_type TEXT NOT NULL,             -- 'mining', 'staking', 'airdrop', 'fork'
    tax_lot_id INT REFERENCES tax_lots(id),
    raw_transaction_id INT REFERENCES tax_raw_transactions(id),
    needs_review BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE when fmv=0 with no price feed
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_ordinary_income_account_symbol
    ON tax_ordinary_income(account_id, symbol, received_at);
CREATE INDEX IF NOT EXISTS idx_tax_ordinary_income_type_year
    ON tax_ordinary_income(income_type, received_at);
CREATE INDEX IF NOT EXISTS idx_tax_ordinary_income_review
    ON tax_ordinary_income(needs_review) WHERE needs_review = TRUE;
