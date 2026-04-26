-- Tax Engine Migration 005 - wash-sale pattern detection column
-- Run:  psql -d ponyboy -f tax/migrations_005_wash_sale.sql
--
-- Closes roadmap item 1.4.  IRC section 1091 reaches "stock or
-- securities", and digital assets are property under Notice 2014-21;
-- practitioner consensus (and the conservative-for-the-taxpayer
-- position) is that wash-sale basis adjustments do not apply to crypto
-- today.  But the pattern should still be recorded so a policy switch
-- is a one-line change and the taxpayer's loss-harvest patterns stay
-- observable in the audit trail.
--
-- Additive: new nullable column on tax_disposals.  tax_lots keeps its
-- wash_sale_basis_adjustment column (already present) so the apply-mode
-- regression path still works when APPLY_WASH_SALE_ADJUSTMENT = True.

ALTER TABLE tax_disposals
    ADD COLUMN IF NOT EXISTS wash_sale_pattern_detected BOOLEAN
        NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_tax_disposals_wash_pattern
    ON tax_disposals(wash_sale_pattern_detected)
    WHERE wash_sale_pattern_detected = TRUE;
