-- Tax Engine Migration 008 - asset family classification for wraps / stables
-- Run:  psql -d ponyboy -f tax/migrations_008_asset_families.sql
--
-- Closes roadmap item 2.2.  USDC<->USDT, BTC<->WBTC, ETH<->WETH and
-- similar same-family swaps are realization events under the
-- conservative reading of file 1 sec 1.10 (no IRS primary guidance on
-- wraps; Notice 2024-57 is procedural).  We must report them on Form
-- 8949 even though the gain/loss is typically near $0; suppression is
-- opt-in at report time.
--
-- Additive: new tax_asset_families table + nullable family flag on
-- tax_lots and tax_disposals so the engine can mark a swap leg without
-- changing the underlying lot/disposal accounting.

CREATE TABLE IF NOT EXISTS tax_asset_families (
    id SERIAL PRIMARY KEY,
    family TEXT NOT NULL,         -- 'usd_stable', 'btc_wrap', 'eth_wrap'
    symbol TEXT NOT NULL,         -- 'USDC', 'WBTC', 'WETH', ...
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (symbol)
);

CREATE INDEX IF NOT EXISTS idx_tax_asset_families_family
    ON tax_asset_families(family);

-- Mark a lot or disposal as one leg of a same-family swap.  NULL means
-- "not part of a wrap/stable swap"; the value is the family name of
-- both legs ('usd_stable', 'btc_wrap', 'eth_wrap').
ALTER TABLE tax_lots
    ADD COLUMN IF NOT EXISTS wrap_family TEXT;

ALTER TABLE tax_disposals
    ADD COLUMN IF NOT EXISTS wrap_family TEXT;

-- Seed the canonical families.  Insertion is idempotent on (symbol).
INSERT INTO tax_asset_families (family, symbol, notes) VALUES
    ('usd_stable', 'USDC', 'Circle USD stablecoin'),
    ('usd_stable', 'USDT', 'Tether USD stablecoin'),
    ('usd_stable', 'DAI',  'MakerDAO USD stablecoin'),
    ('usd_stable', 'USDP', 'Pax Dollar (Paxos) USD stablecoin'),
    ('usd_stable', 'BUSD', 'Binance USD (legacy)'),
    ('usd_stable', 'TUSD', 'TrueUSD'),
    ('btc_wrap',   'BTC',  'native Bitcoin'),
    ('btc_wrap',   'WBTC', 'wrapped BTC on EVM chains'),
    ('btc_wrap',   'cbBTC','Coinbase wrapped BTC'),
    ('eth_wrap',   'ETH',  'native Ether'),
    ('eth_wrap',   'WETH', 'wrapped Ether (ERC-20)'),
    ('eth_wrap',   'stETH','Lido staked ETH (rebasing/wrap-like)')
ON CONFLICT (symbol) DO NOTHING;
