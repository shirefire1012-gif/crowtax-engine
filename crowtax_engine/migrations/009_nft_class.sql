-- Tax Engine Migration 009 - NFT classification framework
-- Run:  psql -d ponyboy -f tax/migrations_009_nft_class.sql
--
-- Closes roadmap item 2.4 (framework only - taxpayer does not currently
-- trade NFTs).  IRS Notice 2023-27 indicates certain NFTs may be
-- "collectibles" under IRC sec 408(m); collectible long-term gains are
-- taxed at the 28% max rate per IRC sec 1(h)(4).
--
-- Additive: new NOT-NULL DEFAULT column on tax_lots with CHECK
-- enforcing the three legal values.  Default 'fungible' so all
-- existing lots and any future fungible-crypto lot continue to flow
-- through the generic Schedule D path.

ALTER TABLE tax_lots
    ADD COLUMN IF NOT EXISTS asset_class TEXT NOT NULL DEFAULT 'fungible';

ALTER TABLE tax_lots
    DROP CONSTRAINT IF EXISTS chk_tax_lots_asset_class;

ALTER TABLE tax_lots
    ADD CONSTRAINT chk_tax_lots_asset_class
    CHECK (asset_class IN ('fungible', 'nft_collectible',
                           'nft_non_collectible'));

CREATE INDEX IF NOT EXISTS idx_tax_lots_asset_class
    ON tax_lots(asset_class)
    WHERE asset_class <> 'fungible';
