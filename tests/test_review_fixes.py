"""Tests added in response to the Task 2 code review: Poisson CI, the gap-aware exceedance
frequency fix, loss-given-shock curves (no probability), and irm.apr vs irm.apy.
"""

import numpy as np
import pytest

from kvr import irm, scenarios, stress
from kvr import morpho_math as mm


def test_poisson_rate_ci_zero_count_lower_bound_is_zero():
    lower, upper = scenarios.poisson_rate_ci(0, years_covered=6.83, confidence=0.90)
    assert lower == 0.0
    assert upper > 0  # absence of evidence isn't evidence of absence -- upper bound is nonzero


def test_poisson_rate_ci_widens_with_fewer_years():
    _l1, u1 = scenarios.poisson_rate_ci(1, years_covered=10.0)
    _l2, u2 = scenarios.poisson_rate_ci(1, years_covered=1.0)
    assert u2 > u1  # same single observed event, shorter window -> wider uncertainty on the rate


def test_exceedance_frequency_excludes_gap_spanning_returns():
    # A long run of flat, regular daily points (padding past the horizon_days+10 minimum-sample
    # floor), then a real 3-day gap spanning a huge price move that must NOT be read as a 1-day
    # return just because it's the next point by index.
    day = 86400
    prices = [(i * day, 100.0) for i in range(15)]
    prices.append((prices[-1][0] + 4 * day, 50.0))  # 4 real days later, price halves
    prices.append((prices[-1][0] + day, 49.0))

    lam = scenarios.exceedance_frequency(prices, horizon_days=1, declustering_days=1)
    assert lam is not None
    # The flat->50.0 jump spans 4 real days, not 1 -- must be excluded from the 1-day return set.
    assert lam.declustered.min() > -0.3


def test_loss_given_shock_curve_hand_computed():
    # Single position, lltv irrelevant to this function (only used for liquidatability elsewhere).
    # collateral=1e8 raw units, price=1e36 (1:1), borrowed=100 (total_borrow_assets=total_borrow_shares=100).
    positions = [{"user": "a", "borrow_shares": 100, "collateral": 10**8}]
    curve = stress.loss_given_shock_curve(positions, lltv=0, oracle_price=10**36, total_borrow_assets=100, total_borrow_shares=100, total_supply_assets=1000, shocks=(-0.5,))
    # At -50% shock: collateral_value = 1e8 * 0.5e36 // 1e36 = 5e7. borrowed=100 (tiny vs collateral) -> loss=0.
    assert curve[-0.5] == 0.0

    # Now make borrowed dominate collateral value so a real shortfall appears.
    positions2 = [{"user": "a", "borrow_shares": 100, "collateral": 1}]  # collateral raw=1, negligible value
    curve2 = stress.loss_given_shock_curve(positions2, lltv=0, oracle_price=10**36, total_borrow_assets=100, total_borrow_shares=100, total_supply_assets=1000, shocks=(-0.5,))
    borrowed = mm.to_assets_up(100, 100, 100)
    expected_loss_frac = borrowed / 1000  # collateral value ~= 0, so full debt is the shortfall
    assert abs(curve2[-0.5] - expected_loss_frac) < 1e-9


def test_apr_does_not_blow_up_like_apy_for_an_extreme_rate():
    # rateAtTarget pinned at MAX_RATE_AT_TARGET-equivalent magnitude, curve at u=1 (4x).
    rate_at_target = irm.MAX_RATE_AT_TARGET
    rate, _end_rt = irm.borrow_rate(irm.WAD, rate_at_target, 0)  # u=1 -> 4x rateAtTarget
    apy = irm.apy_from_per_second_rate(rate)
    apr = irm.apr_from_per_second_rate(rate)
    assert apr == pytest.approx(8.0, rel=1e-6)  # 4 * 2.0/yr = 8.0 = 800%, exactly matches the IRM's own ceiling
    assert apy > 1000  # continuous compounding of the same rate over a year is wildly larger
