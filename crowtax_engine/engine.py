"""Cost basis matching engine: FIFO, LIFO, HIFO, Specific ID."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import psycopg2.extras

from crowtax_engine.models import LotMatch

log = logging.getLogger(__name__)

# Round to the precision of NUMERIC(20,6).
_SIX_PLACES = Decimal("0.000001")

# Sort orders for each matching method. The values are interpolated into
# SQL via an f-string, so VALIDATE_METHOD below is the authoritative guard
# keeping that interpolation safe — never accept an unvalidated ``method``.
METHOD_ORDER = {
    "fifo": "acquired_at ASC",
    "lifo": "acquired_at DESC",
    "hifo": "cost_basis_per_unit DESC",
}

VALID_METHODS = frozenset(("fifo", "lifo", "hifo", "specific_id"))

# Item 1.4: IRC section 1091 wash-sale reaches "stock or securities"; digital
# assets are property under Notice 2014-21 and thus - under the plain
# statutory text - are not subject to section 1091.  Until Congress or
# Treasury extends the rule to crypto, the correct position for a filing
# today is to DETECT the pattern (so the audit trail is complete and a
# policy switch is trivial) but NOT APPLY the basis adjustment.
#
# Flipping this flag to True reproduces the pre-1.4 behaviour exactly
# and is used as a regression anchor in the test suite.  See
# 04-three-decisions-recommendation.md (decision 2) for the legal
# analysis.
APPLY_WASH_SALE_ADJUSTMENT: bool = False


def _validate_method(method: str) -> None:
    """Reject unknown matching methods before any SQL is interpolated."""
    if method not in VALID_METHODS:
        raise ValueError(
            f"Invalid matching method {method!r}. "
            f"Expected one of {sorted(VALID_METHODS)}."
        )


def _epoch_to_date(epoch_seconds: int) -> datetime:
    """Convert epoch seconds to a date for holding period calculation."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date()


def _holding_period(acquired_at: int, disposed_at: int) -> str:
    """Determine holding period using calendar date comparison.

    IRS rule: "more than one year" means acquired 2026-01-15 becomes
    long-term on 2027-01-16 (day after the anniversary).
    """
    acq_date = _epoch_to_date(acquired_at)
    disp_date = _epoch_to_date(disposed_at)
    try:
        anniversary = acq_date.replace(year=acq_date.year + 1)
    except ValueError:
        # Leap day: Feb 29 → Mar 1 of next year
        anniversary = acq_date.replace(year=acq_date.year + 1, month=3, day=1)
    if disp_date > anniversary:
        return "long"
    return "short"


def match_disposal(conn, disposal_id: int, method: str = "fifo",
                   specific_lot_ids: list = None) -> list:
    """Match a disposal against available lots using the specified method.

    Returns list of LotMatch objects created.
    """
    _validate_method(method)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Load the disposal
        cur.execute("SELECT * FROM tax_disposals WHERE id = %s", (disposal_id,))
        disposal = cur.fetchone()
        if not disposal:
            raise ValueError(f"Disposal {disposal_id} not found")

        symbol = disposal["symbol"]
        remaining_to_match = Decimal(str(disposal["quantity"]))
        # Item 1.3: sell-side fees reduce proceeds.  IRC section 1001(b)
        # and Treas. Reg. section 1.1001-1 treat the amount realized as
        # net of selling expenses; Commissioner v. Woodward, 397 U.S. 572
        # (1970) confirms the inverse symmetry of buy-side fees
        # (which increase basis at lot creation in staging.py).
        gross_proceeds = Decimal(str(disposal["proceeds_usd"]))
        sell_fee = Decimal(str(disposal.get("fee_usd") or 0))
        net_proceeds = gross_proceeds - sell_fee
        proceeds_per_unit = (
            net_proceeds / remaining_to_match
            if remaining_to_match > 0 else Decimal(0)
        )

        # Get available lots
        # Item 1.1: per-wallet basis.  When the disposal carries an
        # account_id (populated post migration 003), only consume lots
        # in the same account.  When it is NULL (legacy pre-backfill
        # rows) fall back to the universal pool so historical reports
        # can still be reconstructed.
        account_id = disposal.get("account_id")

        if method == "specific_id" and specific_lot_ids:
            placeholders = ",".join(["%s"] * len(specific_lot_ids))
            if account_id is not None:
                cur.execute(f"""
                    SELECT * FROM tax_lots
                    WHERE id IN ({placeholders})
                      AND symbol = %s
                      AND account_id = %s
                      AND remaining_quantity > 0
                    ORDER BY array_position(%s, id)
                """, list(specific_lot_ids) + [symbol, account_id,
                                               list(specific_lot_ids)])
            else:
                cur.execute(f"""
                    SELECT * FROM tax_lots
                    WHERE id IN ({placeholders})
                      AND symbol = %s
                      AND remaining_quantity > 0
                    ORDER BY array_position(%s, id)
                """, list(specific_lot_ids) + [symbol,
                                               list(specific_lot_ids)])
        else:
            order = METHOD_ORDER.get(method, METHOD_ORDER["fifo"])
            if account_id is not None:
                cur.execute(f"""
                    SELECT * FROM tax_lots
                    WHERE symbol = %s
                      AND account_id = %s
                      AND remaining_quantity > 0
                    ORDER BY {order}
                """, (symbol, account_id))
            else:
                cur.execute(f"""
                    SELECT * FROM tax_lots
                    WHERE symbol = %s
                      AND account_id IS NULL
                      AND remaining_quantity > 0
                    ORDER BY {order}
                """, (symbol,))

        lots = cur.fetchall()
        matches = []

        for lot in lots:
            if remaining_to_match <= 0:
                break

            lot_remaining = Decimal(str(lot["remaining_quantity"]))
            matched_qty = min(remaining_to_match, lot_remaining)

            cost_basis = matched_qty * Decimal(str(lot["cost_basis_per_unit"]))
            proceeds = matched_qty * proceeds_per_unit
            gain_loss = proceeds - cost_basis
            hp = _holding_period(lot["acquired_at"], disposal["disposed_at"])

            cur.execute("""
                INSERT INTO tax_lot_matches
                    (disposal_id, lot_id, quantity_matched, cost_basis_usd,
                     proceeds_usd, gain_loss_usd, holding_period, method)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (disposal_id, lot["id"], matched_qty, cost_basis,
                  proceeds, gain_loss, hp, method))
            match_id = cur.fetchone()["id"]

            # Update lot remaining quantity
            new_remaining = lot_remaining - matched_qty
            cur.execute("""
                UPDATE tax_lots SET remaining_quantity = %s WHERE id = %s
            """, (new_remaining, lot["id"]))

            matches.append(LotMatch(
                id=match_id,
                disposal_id=disposal_id,
                lot_id=lot["id"],
                quantity_matched=matched_qty,
                cost_basis_usd=cost_basis,
                proceeds_usd=proceeds,
                gain_loss_usd=gain_loss,
                holding_period=hp,
                method=method,
            ))

            remaining_to_match -= matched_qty

        if remaining_to_match > 0:
            log.warning(
                "Disposal %d: %.18f %s unmatched (no available lots)",
                disposal_id, remaining_to_match, symbol)

        conn.commit()
        return matches
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _clear_wash_sale(cur, disposal_id: int) -> None:
    """Zero out the wash sale fields on a disposal (no adjustment applied).

    Clears both the detection column (``wash_sale_pattern_detected``) and
    the application columns (``wash_sale_flag``,
    ``wash_sale_disallowed_loss``) so a rematch starts from a clean slate.
    """
    cur.execute(
        """
        UPDATE tax_disposals
        SET wash_sale_pattern_detected = FALSE,
            wash_sale_flag = FALSE,
            wash_sale_disallowed_loss = 0
        WHERE id = %s
        """,
        (disposal_id,),
    )


def check_wash_sales(conn, symbol: str, disposal_id: int) -> bool:
    """Detect the IRS section 1091 wash-sale pattern on a disposal.

    Detection always runs.  Application is gated on
    ``APPLY_WASH_SALE_ADJUSTMENT`` (item 1.4 policy split) - crypto is
    property under Notice 2014-21, and section 1091 by its terms reaches
    "stock or securities", so the default engine posture is to record
    the pattern but NOT adjust basis.

    When a pattern is found:
      - ``tax_disposals.wash_sale_pattern_detected`` is always set TRUE
        so reports can surface the pattern for the CPA's review.

    When the pattern is found AND ``APPLY_WASH_SALE_ADJUSTMENT`` is True:
      - ``tax_disposals.wash_sale_flag`` = TRUE
      - ``tax_disposals.wash_sale_disallowed_loss`` = total disallowed
      - ``tax_lots.wash_sale_basis_adjustment`` accumulates the per-lot
        adjustment so ``rematch_all`` can reset it idempotently
      - ``tax_lots.cost_basis_usd`` / ``cost_basis_per_unit`` are updated
        in place so subsequent matches use the adjusted basis

    Returns True iff the pattern was detected (whether or not the
    adjustment was applied) - so reports can count pattern-detected
    disposals even when basis is unchanged.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT disposed_at, quantity, account_id FROM tax_disposals WHERE id = %s",
            (disposal_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        disposed_at, disposal_qty, disp_account_id = row
        disposal_qty = Decimal(str(disposal_qty))

        # Compute realised net gain/loss from the existing matches.
        cur.execute(
            """
            SELECT COALESCE(SUM(quantity_matched), 0),
                   COALESCE(SUM(gain_loss_usd), 0)
            FROM tax_lot_matches
            WHERE disposal_id = %s
            """,
            (disposal_id,),
        )
        matched_qty, net_gain_loss = cur.fetchone()
        matched_qty = Decimal(str(matched_qty))
        net_gain_loss = Decimal(str(net_gain_loss))

        # Wash sale rule only bites on losses. Clear any stale flag if gain.
        if matched_qty <= 0 or net_gain_loss >= 0 or disposal_qty <= 0:
            _clear_wash_sale(cur, disposal_id)
            conn.commit()
            return False

        loss = -net_gain_loss  # positive dollar amount disallowed up to qty cap

        disp_date = _epoch_to_date(disposed_at)
        window_start = disp_date - timedelta(days=30)
        window_end = disp_date + timedelta(days=30)
        start_epoch = int(datetime(window_start.year, window_start.month,
                                   window_start.day,
                                   tzinfo=timezone.utc).timestamp())
        end_epoch = int(datetime(window_end.year, window_end.month,
                                 window_end.day, 23, 59, 59,
                                 tzinfo=timezone.utc).timestamp())

        # Replacement lots: any lot of the same symbol acquired in the 30-day
        # window. We intentionally DO NOT exclude lots with acquired_at equal
        # to the disposal's timestamp — a same-second repurchase is still a
        # wash sale (code_review_2026_03_31.md #2).
        # Item 1.1: scope replacement-lot search to the same account when
        # the disposal is account-scoped.  Without this, a loss on one
        # wallet could be "washed" by a purchase on a different wallet —
        # which is wrong both economically and under §1091's "taxpayer"
        # framing when layered onto per-wallet basis.
        if disp_account_id is not None:
            cur.execute(
                """
                SELECT id, quantity
                FROM tax_lots
                WHERE symbol = %s
                  AND account_id = %s
                  AND acquired_at BETWEEN %s AND %s
                ORDER BY acquired_at ASC, id ASC
                """,
                (symbol, disp_account_id, start_epoch, end_epoch),
            )
        else:
            cur.execute(
                """
                SELECT id, quantity
                FROM tax_lots
                WHERE symbol = %s
                  AND acquired_at BETWEEN %s AND %s
                ORDER BY acquired_at ASC, id ASC
                """,
                (symbol, start_epoch, end_epoch),
            )
        replacement_lots = cur.fetchall()
        if not replacement_lots:
            _clear_wash_sale(cur, disposal_id)
            conn.commit()
            return False

        total_replacement_qty = sum(
            Decimal(str(q)) for _, q in replacement_lots
        )
        disallowed_qty = min(disposal_qty, total_replacement_qty)
        if disallowed_qty <= 0:
            _clear_wash_sale(cur, disposal_id)
            conn.commit()
            return False

        disallowed_loss = (loss * disallowed_qty / disposal_qty).quantize(
            _SIX_PLACES, rounding=ROUND_HALF_UP
        )
        if disallowed_loss <= 0:
            _clear_wash_sale(cur, disposal_id)
            conn.commit()
            return False

        # Pattern detected - mark the disposal even if we are not going
        # to apply the basis adjustment below.  This lets reports show
        # pattern counts + hypothetical disallowance without changing
        # the underlying gain math.
        cur.execute(
            """
            UPDATE tax_disposals
            SET wash_sale_pattern_detected = TRUE
            WHERE id = %s
            """,
            (disposal_id,),
        )

        if not APPLY_WASH_SALE_ADJUSTMENT:
            # Clear any stale application state left by a prior run
            # under the legacy apply-mode flag, but keep the pattern
            # flag set above.
            cur.execute(
                """
                UPDATE tax_disposals
                SET wash_sale_flag = FALSE,
                    wash_sale_disallowed_loss = 0
                WHERE id = %s
                """,
                (disposal_id,),
            )
            conn.commit()
            log.info(
                "Wash sale pattern detected on disposal %d (%s): $%s "
                "would be disallowed under apply-mode; "
                "APPLY_WASH_SALE_ADJUSTMENT=False, no basis change made",
                disposal_id, symbol, disallowed_loss,
            )
            return True

        # Distribute the disallowed loss across replacement lots in FIFO
        # order by acquisition time. Last allocation absorbs rounding drift.
        remaining_qty = disallowed_qty
        remaining_loss = disallowed_loss
        for lot_id, lot_qty in replacement_lots:
            if remaining_qty <= 0:
                break
            lot_qty = Decimal(str(lot_qty))
            portion = min(remaining_qty, lot_qty)

            is_last = (portion >= remaining_qty)
            if is_last:
                lot_adjustment = remaining_loss
            else:
                lot_adjustment = (
                    disallowed_loss * portion / disallowed_qty
                ).quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)

            if lot_adjustment <= 0:
                remaining_qty -= portion
                continue

            cur.execute(
                """
                UPDATE tax_lots
                SET cost_basis_usd = cost_basis_usd + %s,
                    cost_basis_per_unit = CASE
                        WHEN quantity > 0
                        THEN ((cost_basis_usd + %s) / quantity)
                        ELSE cost_basis_per_unit
                    END,
                    wash_sale_basis_adjustment =
                        wash_sale_basis_adjustment + %s
                WHERE id = %s
                """,
                (lot_adjustment, lot_adjustment, lot_adjustment, lot_id),
            )
            remaining_qty -= portion
            remaining_loss -= lot_adjustment

        cur.execute(
            """
            UPDATE tax_disposals
            SET wash_sale_flag = TRUE,
                wash_sale_disallowed_loss = %s
            WHERE id = %s
            """,
            (disallowed_loss, disposal_id),
        )
        conn.commit()
        log.info(
            "Wash sale applied to disposal %d (%s): $%s disallowed",
            disposal_id, symbol, disallowed_loss,
        )
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def rematch_all(conn, method: str = "fifo") -> dict:
    """Wipe all matches, reset lot and disposal state, and re-match everything.

    Returns a summary dict::

        {
            "method": str,
            "disposals": int,         # total disposals processed
            "matched_count": int,     # disposals with >=1 match row
            "unmatched_count": int,   # disposals with no matches (no lots)
            "total_gain_loss": Decimal,  # sum of gain_loss_usd across matches
            "wash_sale_count": int,   # disposals flagged as wash sales
        }

    Resetting ``wash_sale_basis_adjustment`` back into ``cost_basis_usd`` makes
    repeated calls idempotent — without this, the adjustment would compound on
    each rebuild.
    """
    _validate_method(method)
    cur = conn.cursor()
    try:
        # Delete all existing matches.
        cur.execute("DELETE FROM tax_lot_matches")

        # Undo any prior wash-sale cost-basis adjustments so we start from
        # the original basis on every lot. Using old-column values on the
        # RHS is safe (Postgres SET clause sees pre-UPDATE values).
        cur.execute(
            """
            UPDATE tax_lots
            SET cost_basis_usd = cost_basis_usd - wash_sale_basis_adjustment,
                cost_basis_per_unit = CASE
                    WHEN quantity > 0
                    THEN ((cost_basis_usd - wash_sale_basis_adjustment) / quantity)
                    ELSE cost_basis_per_unit
                END,
                wash_sale_basis_adjustment = 0,
                remaining_quantity = quantity
            """
        )

        # Reset wash sale flags on disposals.  Clear both the detection
        # column (item 1.4) and the application columns so the rematch
        # is reproducible under either policy posture.
        cur.execute(
            """
            UPDATE tax_disposals
            SET wash_sale_pattern_detected = FALSE,
                wash_sale_flag = FALSE,
                wash_sale_disallowed_loss = 0
            """
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

    # Get all disposals in chronological order.
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, symbol FROM tax_disposals ORDER BY disposed_at ASC, id ASC"
        )
        disposals = cur.fetchall()
    finally:
        cur.close()

    matched_count = 0
    unmatched_count = 0
    wash_sale_count = 0
    for disposal_id, symbol in disposals:
        matches = match_disposal(conn, disposal_id, method)
        if matches:
            matched_count += 1
        else:
            unmatched_count += 1
        if check_wash_sales(conn, symbol, disposal_id):
            wash_sale_count += 1

    # Total gain/loss across all matches we just produced.
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COALESCE(SUM(gain_loss_usd), 0) FROM tax_lot_matches WHERE method = %s",
            (method,),
        )
        total_gain_loss = Decimal(str(cur.fetchone()[0]))
        # Commit so callers inherit a clean transaction state (this SELECT
        # implicitly opens a transaction under autocommit=False).
        conn.commit()
    finally:
        cur.close()

    # Item 1.4: under the default APPLY_WASH_SALE_ADJUSTMENT=False
    # posture, ``wash_sale_count`` is the count of pattern detections
    # rather than actual basis adjustments.  Surface both numbers so
    # reports can distinguish.
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM tax_disposals WHERE wash_sale_flag = TRUE"
        )
        wash_sale_applied = int(cur.fetchone()[0])
    finally:
        cur.close()

    summary = {
        "method": method,
        "disposals": len(disposals),
        "matched_count": matched_count,
        "unmatched_count": unmatched_count,
        "total_gain_loss": total_gain_loss,
        "wash_sale_count": wash_sale_count,
        "wash_sale_pattern_detected": wash_sale_count,
        "wash_sale_applied": wash_sale_applied,
        "wash_sale_policy": (
            "apply" if APPLY_WASH_SALE_ADJUSTMENT else "detect_only"
        ),
    }
    log.info(
        "Rematched %d disposals (method=%s): matched=%d unmatched=%d wash=%d total_gl=%s",
        len(disposals), method, matched_count, unmatched_count,
        wash_sale_count, total_gain_loss,
    )
    return summary
