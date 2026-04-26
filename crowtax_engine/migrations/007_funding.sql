-- Tax Engine Migration 007 - perpetual funding events
-- Run:  psql -d ponyboy -f tax/migrations_007_funding.sql
--
-- Closes roadmap item 1.6.  Funding payments on perpetual-futures
-- positions (Hyperliquid / Bybit / OKX / EdgeX) have no IRS primary
-- guidance.  The practitioner consensus (CoinTracker, TokenTax,
-- Green Trader Tax, Awaken) treats funding as ordinary income / expense
-- at time of payment.  Funding received in USDC is IRC section 61 gross
-- income valued at FMV (USDC at par = $1).  This is the engine's
-- default - verify with CPA before filing; see DECISIONS.md.
--
-- Additive: new tax_funding_events table.  No changes to existing
-- tables.

CREATE TABLE IF NOT EXISTS tax_funding_events (
    id SERIAL PRIMARY KEY,
    account_id INT REFERENCES tax_accounts(id),
    symbol_perp TEXT NOT NULL,             -- e.g. 'BTC-PERP', 'HYPE-PERP'
    funding_at BIGINT NOT NULL,            -- epoch seconds of the payment
    funding_usd NUMERIC(20, 6) NOT NULL,   -- signed; positive = received, negative = paid
    direction TEXT NOT NULL,               -- 'received' or 'paid'
    settlement_symbol TEXT,                -- settlement-asset symbol; e.g. 'USDC'
    raw_transaction_id INT REFERENCES tax_raw_transactions(id),
    tax_lot_id INT REFERENCES tax_lots(id),  -- USDC lot created when funding settled on-exchange
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT chk_funding_direction CHECK (direction IN ('received', 'paid'))
);

CREATE INDEX IF NOT EXISTS idx_tax_funding_account_symbol
    ON tax_funding_events(account_id, symbol_perp, funding_at);
CREATE INDEX IF NOT EXISTS idx_tax_funding_direction_year
    ON tax_funding_events(direction, funding_at);
