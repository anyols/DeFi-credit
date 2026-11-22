"""Task 7: validation tests for basis.py -- a hand-computed daily carry, plus the depeg-to-
liquidation and perp-margin-buffer risk formulas. Chain-free and deterministic, like T7's own
toy cases: no real API/chain calls, hand-built inputs only.
"""

import pytest

from kvr import basis, irm


def test_daily_carry_hand_computed():
    # 3 funding events on day 0: +0.01%, +0.02%, -0.005% -> daily sum = 0.00025, annualized *365.
    funding_history = [(0, 0.0001), (28800, 0.0002), (57600, -0.00005)]
    # Staking: wstETH's own rate grows 1.000 -> 1.001 over a 14-day bracket (0.1% in 14 days).
    staking_samples = [(0, 1.0), (14 * 86400, 1.001)]
    # u=0.9 exactly: AdaptiveCurveIrm's own curve collapses to borrow_rate_per_second ==
    # rate_at_target (same hand-derivable property used in tests/test_backtest.py).
    rate_at_target = 1_761_517_973  # ~5.56%/yr at this exact value
    wsteth_morpho_history = {0: {"utilization": 0.9, "rateAtTarget": rate_at_target}}

    result = basis.daily_carry(0, funding_history, staking_samples, wsteth_morpho_history, perp_fee_bps=8, target_ltv=0.70)

    expected_funding_annual = (0.0001 + 0.0002 - 0.00005) * 365
    expected_staking_annual = (1.001 / 1.0 - 1) * (365 * 86400 / (14 * 86400))
    expected_morpho_apr = rate_at_target / 10**18 * irm.SECONDS_PER_YEAR
    expected_fee_drag = (8 / 10_000) * (365 / 548)
    expected_basis_carry = expected_staking_annual + expected_funding_annual - expected_fee_drag
    leverage = 1 / (1 - 0.70)
    expected_levered_carry = leverage * (expected_staking_annual + expected_funding_annual) - (leverage - 1) * expected_morpho_apr - expected_fee_drag

    assert result["funding_annual"] == pytest.approx(expected_funding_annual)
    assert result["staking_annual"] == pytest.approx(expected_staking_annual)
    assert result["morpho_borrow_apr"] == pytest.approx(expected_morpho_apr)
    assert result["basis_carry"] == pytest.approx(expected_basis_carry)
    assert result["levered_carry"] == pytest.approx(expected_levered_carry)


def test_daily_carry_none_when_funding_data_missing():
    # No funding event recorded for day=99999 -- must stay None, never silently zeroed.
    result = basis.daily_carry(99999, [(0, 0.0001)], [(0, 1.0), (14 * 86400, 1.001)], {})
    assert result["funding_annual"] is None
    assert result["basis_carry"] is None
    assert result["levered_carry"] is None


def test_depeg_to_liquidation_hand_computed():
    # At target_ltv=0.70 and lltv=0.86 (this project's real WBTC/wstETH-class LLTV), wstETH's
    # value can fall by 1 - 0.70/0.86 = 18.605% before the levered position is liquidated.
    assert basis.depeg_to_liquidation(0.70, 0.86) == pytest.approx(1 - 0.70 / 0.86)
    assert basis.depeg_to_liquidation(0.70, 0.86) == pytest.approx(0.186046511627907)


def test_perp_margin_buffer_needed_hand_computed():
    # At 3.3333x leverage (target_ltv=0.70), a +10% ETH move costs 0.3333 of the trade's own
    # equity on the short perp leg (notional moves 1:1, and notional = leverage * equity).
    leverage = 1 / (1 - 0.70)
    assert basis.perp_margin_buffer_needed(leverage, 0.10) == pytest.approx(leverage * 0.10)
    assert basis.perp_margin_buffer_needed(leverage, 0.10) == pytest.approx(0.333333333333333)
