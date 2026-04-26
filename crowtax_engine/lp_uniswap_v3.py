"""Uniswap v3 LP event handler.

Roadmap item 2.3.  Treats Uniswap v3 mint / burn / collect events as
realization events, with deposited / returned tokens recorded at FMV
and uncollected fees as ordinary income on receipt.

File 1 sec 1.10 -- IRS has issued no primary guidance on AMM
liquidity provision for digital assets.  Two competing positions:

    (a) deposit-as-disposition: depositing token0/token1 into the
        pool is a realization event; the LP NFT is a new property
        with basis = sum of FMV of deposited tokens.  Withdrawal is
        another disposition (the LP NFT is exchanged for the
        returned tokens at FMV).  Fees collected separately are
        ordinary income.

    (b) custodial-pool: the deposit is a non-event because the
        depositor retains beneficial ownership; only fees are
        income; withdrawal is a non-event.

This module implements position (a) -- the conservative,
practitioner-consensus reading.  It is uncertain -- verify with a
CPA before filing.  Position (b) can be enabled via a future flag
when CPA guidance lands; the dispatcher would then no-op on
mint/burn and only handle collect.

Scope: Uniswap v3 ONLY.  Curve, Balancer, SushiSwap, Uniswap v2 are
deferred to a future session (each has different event signatures
and accounting -- conflating them risks wrong basis).

Detection: caller passes us a normalised event dict; we look at
either event_type or the contract address (v3 NFT position
manager: 0xC36442b4a4522E871399CD717aBDD847Ab11FE88) plus the
function selector.  ingest_dex / staging routes raw rows here when
they match -- see is_uniswap_v3_event().
"""

from __future__ import annotations

import logging
from decimal import Decimal

import psycopg2.extras

from crowtax_engine.ordinary_income import record_income

log = logging.getLogger(__name__)

# Uniswap v3 contracts (mainnet).  Other v3 deployments share the
# event ABI but live at different addresses; callers can extend
# UNIV3_POSITION_MANAGERS in their wiring layer.
UNIV3_POSITION_MANAGER = '0xc36442b4a4522e871399cd717abdd847ab11fe88'
UNIV3_POSITION_MANAGERS = frozenset([UNIV3_POSITION_MANAGER])

# Event type tokens we accept on the normalised dict.
EVENT_MINT = 'mint'
EVENT_BURN = 'burn'
EVENT_COLLECT = 'collect'
VALID_EVENTS = frozenset([EVENT_MINT, EVENT_BURN, EVENT_COLLECT])


def is_uniswap_v3_event(raw_row: dict) -> bool:
    '''Detect whether a raw transaction is a Uniswap v3 LP event.

    Returns True when either:
      * raw_row['protocol'] == 'uniswap_v3', or
      * raw_row['contract'] (lowercased) is in
        UNIV3_POSITION_MANAGERS, or
      * raw_row['to'] (lowercased) is in UNIV3_POSITION_MANAGERS.
    '''
    if (raw_row.get('protocol') or '').lower() == 'uniswap_v3':
        return True
    for key in ('contract', 'to', 'address'):
        addr = (raw_row.get(key) or '').lower()
        if addr in UNIV3_POSITION_MANAGERS:
            return True
    return False


def _lp_symbol(token_id) -> str:
    '''Synthetic symbol for a Uniswap v3 LP NFT lot.'''
    return f'UNIV3-LP:{token_id}'


def _insert_lot(conn, *, account_id, chain, symbol, acquired_at,
                quantity, cost_basis_usd, acquisition_type='swap',
                fee_usd=Decimal('0'), source='dex',
                source_tx_id=None, raw_transaction_id=None,
                asset_class='fungible',
                wallet_address=None) -> int:
    quantity = Decimal(str(quantity))
    cost_basis_usd = Decimal(str(cost_basis_usd))
    if quantity > 0:
        cost_per_unit = cost_basis_usd / quantity
    else:
        cost_per_unit = Decimal('0')
    cur = conn.cursor()
    try:
        cur.execute(
            '''
            INSERT INTO tax_lots
                (account_id, wallet_address, chain, symbol,
                 acquired_at, quantity, cost_basis_usd,
                 cost_basis_per_unit, remaining_quantity,
                 acquisition_type, fee_usd, source, source_tx_id,
                 raw_transaction_id, asset_class)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            RETURNING id
            ''',
            (account_id, wallet_address, chain, symbol,
             acquired_at, quantity, cost_basis_usd, cost_per_unit,
             quantity, acquisition_type, Decimal(str(fee_usd)),
             source, source_tx_id, raw_transaction_id, asset_class),
        )
        lot_id = cur.fetchone()[0]
        conn.commit()
        return lot_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _insert_disposal(conn, *, account_id, chain, symbol,
                     disposed_at, quantity, proceeds_usd,
                     fee_usd=Decimal('0'), source='dex',
                     source_tx_id=None, raw_transaction_id=None,
                     wallet_address=None) -> int:
    cur = conn.cursor()
    try:
        cur.execute(
            '''
            INSERT INTO tax_disposals
                (account_id, wallet_address, chain, symbol,
                 disposed_at, quantity, proceeds_usd, fee_usd,
                 source, source_tx_id, raw_transaction_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            ''',
            (account_id, wallet_address, chain, symbol,
             disposed_at, Decimal(str(quantity)),
             Decimal(str(proceeds_usd)),
             Decimal(str(fee_usd)),
             source, source_tx_id, raw_transaction_id),
        )
        dsp_id = cur.fetchone()[0]
        conn.commit()
        return dsp_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def handle_mint(conn, *, account_id, token_id, chain,
                token0_symbol, token0_quantity, token0_fmv_per_unit,
                token1_symbol, token1_quantity, token1_fmv_per_unit,
                event_at, raw_transaction_id=None,
                source_tx_id=None, wallet_address=None) -> dict:
    '''Process a Uniswap v3 mint: 2 token disposals + 1 LP-NFT lot.

    Per file 1 sec 1.10 (uncertain -- verify with a CPA), depositing
    token0 and token1 into the pool is treated as a realization
    event for each token at FMV.  The LP NFT becomes a new lot
    whose basis is the sum of those FMVs.

    Returns ``{'token0_disposal_id': int, 'token1_disposal_id': int,
    'lp_lot_id': int}``.
    '''
    q0 = Decimal(str(token0_quantity))
    q1 = Decimal(str(token1_quantity))
    fmv0 = q0 * Decimal(str(token0_fmv_per_unit))
    fmv1 = q1 * Decimal(str(token1_fmv_per_unit))
    lp_basis = fmv0 + fmv1

    d0 = _insert_disposal(
        conn, account_id=account_id, chain=chain,
        symbol=token0_symbol, disposed_at=event_at,
        quantity=q0, proceeds_usd=fmv0,
        source_tx_id=source_tx_id,
        raw_transaction_id=raw_transaction_id,
        wallet_address=wallet_address,
    )
    d1 = _insert_disposal(
        conn, account_id=account_id, chain=chain,
        symbol=token1_symbol, disposed_at=event_at,
        quantity=q1, proceeds_usd=fmv1,
        source_tx_id=source_tx_id,
        raw_transaction_id=raw_transaction_id,
        wallet_address=wallet_address,
    )
    lp_lot = _insert_lot(
        conn, account_id=account_id, chain=chain,
        symbol=_lp_symbol(token_id), acquired_at=event_at,
        quantity=Decimal('1'),
        cost_basis_usd=lp_basis,
        acquisition_type='swap',
        source_tx_id=source_tx_id,
        raw_transaction_id=raw_transaction_id,
        wallet_address=wallet_address,
        asset_class='fungible',
    )
    return {
        'token0_disposal_id': d0,
        'token1_disposal_id': d1,
        'lp_lot_id': lp_lot,
    }


def handle_burn(conn, *, account_id, token_id, chain,
                token0_symbol, token0_quantity, token0_fmv_per_unit,
                token1_symbol, token1_quantity, token1_fmv_per_unit,
                event_at, raw_transaction_id=None,
                source_tx_id=None, wallet_address=None) -> dict:
    '''Process a Uniswap v3 burn: 1 LP-NFT disposal + 2 token lots.

    Per file 1 sec 1.10 (uncertain -- verify with a CPA), withdrawal
    is the inverse of mint.  The LP NFT is disposed at proceeds =
    sum of FMVs of returned tokens; the returned tokens become new
    lots at those same FMVs.  Net economic gain (impermanent loss
    or gain) flows through the disposal of the LP NFT against its
    sum-of-deposit-FMVs basis.

    Returns ``{'lp_disposal_id': int, 'token0_lot_id': int,
    'token1_lot_id': int}``.  Raises ValueError when no prior LP
    lot exists for ``token_id`` on this account.
    '''
    lp_sym = _lp_symbol(token_id)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            '''
            SELECT id, remaining_quantity FROM tax_lots
            WHERE account_id = %s AND symbol = %s
              AND remaining_quantity > 0
            ORDER BY acquired_at ASC, id ASC
            LIMIT 1
            ''',
            (account_id, lp_sym),
        )
        existing = cur.fetchone()
    finally:
        cur.close()

    if existing is None:
        raise ValueError(
            f'Uniswap v3 burn before mint for token_id={token_id} '
            f'on account_id={account_id}: no prior LP lot found'
        )

    q0 = Decimal(str(token0_quantity))
    q1 = Decimal(str(token1_quantity))
    fmv0 = q0 * Decimal(str(token0_fmv_per_unit))
    fmv1 = q1 * Decimal(str(token1_fmv_per_unit))
    lp_proceeds = fmv0 + fmv1

    lp_dsp = _insert_disposal(
        conn, account_id=account_id, chain=chain,
        symbol=lp_sym, disposed_at=event_at,
        quantity=Decimal('1'), proceeds_usd=lp_proceeds,
        source_tx_id=source_tx_id,
        raw_transaction_id=raw_transaction_id,
        wallet_address=wallet_address,
    )
    lot0 = _insert_lot(
        conn, account_id=account_id, chain=chain,
        symbol=token0_symbol, acquired_at=event_at,
        quantity=q0, cost_basis_usd=fmv0,
        acquisition_type='swap',
        source_tx_id=source_tx_id,
        raw_transaction_id=raw_transaction_id,
        wallet_address=wallet_address,
    )
    lot1 = _insert_lot(
        conn, account_id=account_id, chain=chain,
        symbol=token1_symbol, acquired_at=event_at,
        quantity=q1, cost_basis_usd=fmv1,
        acquisition_type='swap',
        source_tx_id=source_tx_id,
        raw_transaction_id=raw_transaction_id,
        wallet_address=wallet_address,
    )
    return {
        'lp_disposal_id': lp_dsp,
        'token0_lot_id': lot0,
        'token1_lot_id': lot1,
    }


def handle_collect(conn, *, account_id, token_id, chain,
                   fee0_symbol, fee0_quantity, fee0_fmv_per_unit,
                   fee1_symbol, fee1_quantity, fee1_fmv_per_unit,
                   event_at, raw_transaction_id=None,
                   source_tx_id=None) -> list:
    '''Process a Uniswap v3 collect: uncollected fees as ordinary income.

    Per file 1 sec 1.10, fees collected from the position are
    ordinary income on receipt at FMV.  This adds an entry to
    tax_ordinary_income with income_type='lp_fees' for each non-
    zero fee leg.  Zero-fee legs are skipped (the protocol can emit
    a collect with one or both legs zero).

    Returns the list of new tax_ordinary_income.id values.

    The caller is responsible for also creating tax_lots rows for
    the fee tokens at basis = FMV (so a later spot sale of those
    tokens does not double-count the income).  This module emits
    the income row only -- the lot creation lives in the wiring
    layer (staging.promote_confirmed will route the collect-fee
    receive transfers through the normal lot path).
    '''
    out = []
    for sym, qty, fmv_per_unit in (
        (fee0_symbol, fee0_quantity, fee0_fmv_per_unit),
        (fee1_symbol, fee1_quantity, fee1_fmv_per_unit),
    ):
        q = Decimal(str(qty))
        if q <= 0:
            continue
        fmv_total = q * Decimal(str(fmv_per_unit))
        new_id = record_income(
            conn,
            income_type='lp_fees',
            symbol=sym,
            quantity=q,
            received_at=event_at,
            fmv_usd=fmv_total,
            fmv_source='univ3_collect',
            account_id=account_id,
            raw_transaction_id=raw_transaction_id,
            notes=(
                f'Uniswap v3 fee collected on token_id={token_id}'
            ),
        )
        out.append(new_id)
    return out


def dispatch(conn, event: dict) -> dict:
    '''Route a normalised Uniswap v3 event to the right handler.

    Required event keys (caller-normalised):
        type, account_id, chain, token_id, event_at
    Plus mint/burn:
        token0_symbol, token0_quantity, token0_fmv_per_unit,
        token1_symbol, token1_quantity, token1_fmv_per_unit
    Plus collect:
        fee0_symbol, fee0_quantity, fee0_fmv_per_unit,
        fee1_symbol, fee1_quantity, fee1_fmv_per_unit
    '''
    et = (event.get('type') or '').lower()
    if et not in VALID_EVENTS:
        raise ValueError(
            f'Uniswap v3 dispatcher: unknown event type {et!r}; '
            f'expected one of {sorted(VALID_EVENTS)}'
        )
    common = dict(
        account_id=event['account_id'],
        token_id=event['token_id'],
        chain=event.get('chain', 'ETH'),
        event_at=event['event_at'],
        raw_transaction_id=event.get('raw_transaction_id'),
        source_tx_id=event.get('source_tx_id'),
    )
    if et == EVENT_MINT:
        return handle_mint(
            conn,
            wallet_address=event.get('wallet_address'),
            token0_symbol=event['token0_symbol'],
            token0_quantity=event['token0_quantity'],
            token0_fmv_per_unit=event['token0_fmv_per_unit'],
            token1_symbol=event['token1_symbol'],
            token1_quantity=event['token1_quantity'],
            token1_fmv_per_unit=event['token1_fmv_per_unit'],
            **common,
        )
    if et == EVENT_BURN:
        return handle_burn(
            conn,
            wallet_address=event.get('wallet_address'),
            token0_symbol=event['token0_symbol'],
            token0_quantity=event['token0_quantity'],
            token0_fmv_per_unit=event['token0_fmv_per_unit'],
            token1_symbol=event['token1_symbol'],
            token1_quantity=event['token1_quantity'],
            token1_fmv_per_unit=event['token1_fmv_per_unit'],
            **common,
        )
    return {
        'income_ids': handle_collect(
            conn,
            fee0_symbol=event['fee0_symbol'],
            fee0_quantity=event['fee0_quantity'],
            fee0_fmv_per_unit=event['fee0_fmv_per_unit'],
            fee1_symbol=event['fee1_symbol'],
            fee1_quantity=event['fee1_quantity'],
            fee1_fmv_per_unit=event['fee1_fmv_per_unit'],
            **common,
        ),
    }
