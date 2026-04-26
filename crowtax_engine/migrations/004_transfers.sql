-- Tax Engine Migration 004 — wallet-to-wallet transfer ledger
-- Run:  psql -d ponyboy -f tax/migrations_004_transfers.sql
--
-- Closes roadmap item 1.2.  Implements the IRC §1223(2) / Notice 2014-21
-- position that transfers of crypto between wallets/accounts owned by
-- the same taxpayer are non-dispositive: basis and acquired_at travel
-- with the coins, no gain / loss is recognized.  Enforcing this is
-- load-bearing under per-wallet basis (item 1.1), because otherwise
-- every withdraw-deposit pair would register as a disposal at the
-- sending wallet and a new purchase at the receiving wallet with FMV
-- basis — producing a phantom gain.
--
-- The table is append-only: once a transfer is applied it remains in
-- the ledger with status='applied' and a reference to the destination
-- lots it created.  Unmatched outbound (or inbound) legs stay in
-- status='unmatched' and must be resolved by hand — never silently
-- dropped, never silently classified as a sale.

CREATE TABLE IF NOT EXISTS tax_transfers (
    id SERIAL PRIMARY KEY,
    from_account_id INT REFERENCES tax_accounts(id),  -- NULL for unmatched inbound
    to_account_id   INT REFERENCES tax_accounts(id),  -- NULL for unmatched outbound
    symbol TEXT NOT NULL,
    quantity NUMERIC(30, 18) NOT NULL,
    transferred_at BIGINT NOT NULL,              -- epoch seconds; use recv time when both legs known
    fee_usd NUMERIC(20, 6) DEFAULT 0,            -- network / withdrawal fee on the transfer itself
    status TEXT NOT NULL DEFAULT 'unmatched',    -- 'unmatched', 'applied', 'rejected'
    notes TEXT,
    raw_transaction_id INT REFERENCES tax_raw_transactions(id),
    paired_raw_transaction_id INT REFERENCES tax_raw_transactions(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    applied_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tax_transfers_status
    ON tax_transfers(status);
CREATE INDEX IF NOT EXISTS idx_tax_transfers_symbol_time
    ON tax_transfers(symbol, transferred_at);
CREATE INDEX IF NOT EXISTS idx_tax_transfers_from
    ON tax_transfers(from_account_id, symbol, transferred_at);
CREATE INDEX IF NOT EXISTS idx_tax_transfers_to
    ON tax_transfers(to_account_id, symbol, transferred_at);

-- Track, on every lot, whether it was born from a transfer (so we can
-- reconstruct the transfer chain in audit reports and the HDAF export).
ALTER TABLE tax_lots
    ADD COLUMN IF NOT EXISTS transfer_id INT REFERENCES tax_transfers(id);

ALTER TABLE tax_lots
    ADD COLUMN IF NOT EXISTS parent_lot_id INT REFERENCES tax_lots(id);

CREATE INDEX IF NOT EXISTS idx_tax_lots_transfer
    ON tax_lots(transfer_id) WHERE transfer_id IS NOT NULL;
