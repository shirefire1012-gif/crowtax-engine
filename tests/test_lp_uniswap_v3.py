'''Tests for roadmap item 2.3 -- Uniswap v3 LP event handler.

File 1 sec 1.10 -- IRS has issued no primary guidance on AMM
liquidity provision.  We adopt the deposit-as-disposition position
(uncertain -- verify with a CPA).

Covers:
    * Mint: 2 token disposals at FMV + 1 LP-NFT lot at sum-of-FMVs
    * Burn (impermanent loss): LP disposal + 2 fresh token lots,
      net economic outcome verified within $1
    * Collect: uncollected fees -> ordinary income (lp_fees) at
      FMV on receipt
    * Mint then immediate burn with no price movement -> near-zero
      net gain
    * Burn before mint -> clear ValueError
    * is_uniswap_v3_event detection
'''

from __future__ import annotations

from decimal import Decimal

import pytest

from crowtax_engine import engine, lp_uniswap_v3
from tests.builders import make_account


def _epoch(s):
    from datetime import datetime, timezone
    return int(datetime.strptime(s, '%Y-%m-%d').replace(
        tzinfo=timezone.utc).timestamp())


def test_is_uniswap_v3_event_detection():
    assert lp_uniswap_v3.is_uniswap_v3_event({'protocol': 'uniswap_v3'})
    assert lp_uniswap_v3.is_uniswap_v3_event({'protocol': 'UNISWAP_V3'})
    assert lp_uniswap_v3.is_uniswap_v3_event(
        {'to': '0xC36442b4a4522E871399CD717aBDD847Ab11FE88'})
    assert lp_uniswap_v3.is_uniswap_v3_event(
        {'contract': '0xc36442b4a4522e871399cd717abdd847ab11fe88'})
    assert not lp_uniswap_v3.is_uniswap_v3_event(
        {'protocol': 'curve'})
    assert not lp_uniswap_v3.is_uniswap_v3_event(
        {'to': '0x1111111111111111111111111111111111111111'})


def test_mint_creates_two_disposals_and_one_lp_lot(db):
    '''Mint 10 ETH at $3000 + 30000 USDC at $1 -> 2 disposals + 1 LP-NFT lot.'''
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    result = lp_uniswap_v3.handle_mint(
        db, account_id=acc, token_id=42, chain='ETH',
        token0_symbol='ETH', token0_quantity=Decimal('10'),
        token0_fmv_per_unit=Decimal('3000'),
        token1_symbol='USDC', token1_quantity=Decimal('30000'),
        token1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-03-01'),
    )
    assert 'token0_disposal_id' in result
    assert 'token1_disposal_id' in result
    assert 'lp_lot_id' in result

    cur = db.cursor()
    cur.execute(
        'SELECT symbol, quantity, proceeds_usd FROM tax_disposals '
        'ORDER BY symbol')
    rows = cur.fetchall()
    cur.close()
    assert len(rows) == 2
    syms = {r[0]: (Decimal(str(r[1])), Decimal(str(r[2]))) for r in rows}
    assert syms['ETH'] == (Decimal('10'), Decimal('30000'))
    assert syms['USDC'] == (Decimal('30000'), Decimal('30000'))

    cur = db.cursor()
    cur.execute(
        'SELECT symbol, quantity, cost_basis_usd, asset_class '
        "FROM tax_lots WHERE symbol LIKE 'UNIV3-LP:%'")
    lp_rows = cur.fetchall()
    cur.close()
    assert len(lp_rows) == 1
    lp_sym, lp_qty, lp_basis, lp_class = lp_rows[0]
    assert lp_sym == 'UNIV3-LP:42'
    assert Decimal(str(lp_qty)) == Decimal('1')
    assert Decimal(str(lp_basis)) == Decimal('60000')
    assert lp_class == 'fungible'


def test_burn_impermanent_loss_scenario(db):
    '''Mint at ETH $3000; burn after ETH -> $3500.

    Pool has rebalanced: more USDC, less ETH.  After v3 IL math,
    the LP NFT now redeems for, say, 9 ETH (FMV $31500) and
    31500 USDC (FMV 31500).  So lp_proceeds = $63000 vs basis
    of $60000 -> $3000 LP gain (this is the captured IL).  We
    verify the lots arithmetic flows through cleanly.
    '''
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    lp_uniswap_v3.handle_mint(
        db, account_id=acc, token_id=42, chain='ETH',
        token0_symbol='ETH', token0_quantity=Decimal('10'),
        token0_fmv_per_unit=Decimal('3000'),
        token1_symbol='USDC', token1_quantity=Decimal('30000'),
        token1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-03-01'),
    )

    burn_result = lp_uniswap_v3.handle_burn(
        db, account_id=acc, token_id=42, chain='ETH',
        token0_symbol='ETH', token0_quantity=Decimal('9'),
        token0_fmv_per_unit=Decimal('3500'),
        token1_symbol='USDC', token1_quantity=Decimal('31500'),
        token1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-06-01'),
    )
    assert 'lp_disposal_id' in burn_result

    # Match the LP disposal: gain = proceeds (63000) - basis (60000) = 3000.
    cur = db.cursor()
    cur.execute(
        'SELECT id FROM tax_disposals WHERE id = %s',
        (burn_result['lp_disposal_id'],))
    dsp_id = cur.fetchone()[0]
    cur.close()
    matches = engine.match_disposal(db, dsp_id, method='fifo')
    total_gain = sum(Decimal(str(m.gain_loss_usd)) for m in matches)
    assert abs(total_gain - Decimal('3000')) < Decimal('1')

    # Token lots from the burn carry basis equal to FMV at burn.
    cur = db.cursor()
    cur.execute(
        'SELECT symbol, cost_basis_usd FROM tax_lots '
        "WHERE symbol IN ('ETH','USDC') ORDER BY id DESC LIMIT 2")
    new_lots = cur.fetchall()
    cur.close()
    bases = {r[0]: Decimal(str(r[1])) for r in new_lots}
    assert bases['ETH'] == Decimal('31500')
    assert bases['USDC'] == Decimal('31500')


def test_collect_creates_ordinary_income(db):
    '''Collect uncollected fees -> tax_ordinary_income with type lp_fees.'''
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    lp_uniswap_v3.handle_mint(
        db, account_id=acc, token_id=42, chain='ETH',
        token0_symbol='ETH', token0_quantity=Decimal('10'),
        token0_fmv_per_unit=Decimal('3000'),
        token1_symbol='USDC', token1_quantity=Decimal('30000'),
        token1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-03-01'),
    )

    income_ids = lp_uniswap_v3.handle_collect(
        db, account_id=acc, token_id=42, chain='ETH',
        fee0_symbol='ETH', fee0_quantity=Decimal('0.05'),
        fee0_fmv_per_unit=Decimal('3200'),
        fee1_symbol='USDC', fee1_quantity=Decimal('150'),
        fee1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-04-15'),
    )
    assert len(income_ids) == 2

    cur = db.cursor()
    cur.execute(
        'SELECT income_type, symbol, fmv_usd FROM tax_ordinary_income '
        'ORDER BY symbol')
    rows = cur.fetchall()
    cur.close()
    assert len(rows) == 2
    assert all(r[0] == 'lp_fees' for r in rows)
    fmv_by_sym = {r[1]: Decimal(str(r[2])) for r in rows}
    assert fmv_by_sym['ETH'] == Decimal('160')   # 0.05 * 3200
    assert fmv_by_sym['USDC'] == Decimal('150')  # 150 * 1


def test_collect_skips_zero_fee_legs(db):
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    income_ids = lp_uniswap_v3.handle_collect(
        db, account_id=acc, token_id=42, chain='ETH',
        fee0_symbol='ETH', fee0_quantity=Decimal('0'),
        fee0_fmv_per_unit=Decimal('3000'),
        fee1_symbol='USDC', fee1_quantity=Decimal('150'),
        fee1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-04-15'),
    )
    assert len(income_ids) == 1  # only USDC leg recorded


def test_mint_then_immediate_burn_no_price_movement(db):
    '''Round-trip with no price change -> near-zero net capital gain.'''
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    lp_uniswap_v3.handle_mint(
        db, account_id=acc, token_id=99, chain='ETH',
        token0_symbol='ETH', token0_quantity=Decimal('5'),
        token0_fmv_per_unit=Decimal('3000'),
        token1_symbol='USDC', token1_quantity=Decimal('15000'),
        token1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-03-01'),
    )
    burn_result = lp_uniswap_v3.handle_burn(
        db, account_id=acc, token_id=99, chain='ETH',
        token0_symbol='ETH', token0_quantity=Decimal('5'),
        token0_fmv_per_unit=Decimal('3000'),
        token1_symbol='USDC', token1_quantity=Decimal('15000'),
        token1_fmv_per_unit=Decimal('1'),
        event_at=_epoch('2025-03-02'),
    )
    matches = engine.match_disposal(
        db, burn_result['lp_disposal_id'], method='fifo')
    total_gain = sum(Decimal(str(m.gain_loss_usd)) for m in matches)
    assert abs(total_gain) < Decimal('0.01')


def test_burn_before_mint_raises(db):
    '''Burn with no prior LP lot -> ValueError.'''
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    with pytest.raises(ValueError, match='burn before mint'):
        lp_uniswap_v3.handle_burn(
            db, account_id=acc, token_id=999, chain='ETH',
            token0_symbol='ETH', token0_quantity=Decimal('5'),
            token0_fmv_per_unit=Decimal('3000'),
            token1_symbol='USDC', token1_quantity=Decimal('15000'),
            token1_fmv_per_unit=Decimal('1'),
            event_at=_epoch('2025-06-01'),
        )


def test_dispatch_routes_by_event_type(db):
    '''dispatch() picks the right handler from the event dict.'''
    acc = make_account(db, source='onchain',
                       wallet_address='0xlp', chain='ETH')
    mint_result = lp_uniswap_v3.dispatch(db, {
        'type': 'mint',
        'account_id': acc,
        'token_id': 7,
        'chain': 'ETH',
        'event_at': _epoch('2025-03-01'),
        'token0_symbol': 'ETH',
        'token0_quantity': Decimal('1'),
        'token0_fmv_per_unit': Decimal('3000'),
        'token1_symbol': 'USDC',
        'token1_quantity': Decimal('3000'),
        'token1_fmv_per_unit': Decimal('1'),
    })
    assert 'lp_lot_id' in mint_result

    with pytest.raises(ValueError, match='unknown event type'):
        lp_uniswap_v3.dispatch(db, {
            'type': 'swap', 'account_id': acc, 'token_id': 7,
            'event_at': _epoch('2025-03-01'),
        })
