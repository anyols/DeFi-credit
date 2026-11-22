"""stress.run_scenario: regression test for a real bug found while implementing Task 4's review
fix 5 (pulling in the stress engine's "modeled" walk-away shock) -- the old n_stranded counted
every position that never executed, including the vast majority that were simply healthy and
never at risk at all, making n_stranded > 0 (and find_breakpoint_shock's stranding check) true
at almost any shock for any market with more than a handful of positions. Confirmed on real
WBTC data: n_stranded was 637 (the market's ENTIRE position count) even at 0% shock.
"""

import pytest

from kvr import stress
from kvr import morpho_math as mm

PRICE = 10**36
LLTV = int(0.8 * mm.WAD)


def test_n_stranded_excludes_healthy_positions_never_at_risk():
    # 3 positions, total borrow large enough that VIRTUAL_SHARES=1e6 dilution is negligible:
    # P1 healthy (borrowed << max_borrow), P2 liquidatable but stranded (zero collateral, can't
    # seize anything), P3 liquidatable and executed (clear profit margin, zero slippage/gas).
    positions = [
        {"user": "healthy", "borrow_shares": 1_000_000_000, "collateral": 10_000_000_000},
        {"user": "zero_collateral", "borrow_shares": 500_000_000, "collateral": 0},
        {"user": "profitable", "borrow_shares": 900_000, "collateral": 1_000_000},
    ]
    total_borrow = sum(p["borrow_shares"] for p in positions)

    result = stress.run_scenario(
        positions, LLTV, PRICE, PRICE,
        total_borrow_assets=total_borrow, total_borrow_shares=total_borrow,
        market_shock=0.0, oracle_shock_ratio=1.0, kappa=1, path_steps=1,
        slip_ab=(0.0, 1.0), gas_cost_loan_units=0,
    )
    assert result["n_executed"] == 1  # only "profitable"
    # The old buggy version would report n_stranded=2 here (both non-executed positions,
    # including the healthy one that was never liquidatable in the first place).
    assert result["n_stranded"] == 1  # only "zero_collateral" -- "healthy" was never at risk


def test_n_stranded_zero_when_nothing_is_liquidatable():
    positions = [{"user": "healthy", "borrow_shares": 1_000_000_000, "collateral": 10_000_000_000}]
    result = stress.run_scenario(
        positions, LLTV, PRICE, PRICE,
        total_borrow_assets=1_000_000_000, total_borrow_shares=1_000_000_000,
        market_shock=0.0, oracle_shock_ratio=1.0, kappa=1, path_steps=1,
        slip_ab=(0.0, 1.0), gas_cost_loan_units=0,
    )
    assert result["n_executed"] == 0
    assert result["n_stranded"] == 0


def test_n_stranded_counts_a_position_liquidatable_mid_path_but_never_executed():
    # A multi-step path where a position only becomes liquidatable at a LATER step (once the
    # price has moved further) and is then stranded there (zero collateral) -- n_stranded must
    # still count it even though it wasn't at risk at step 1.
    positions = [{"user": "zero_collateral", "borrow_shares": 500_000_000, "collateral": 0}]
    result = stress.run_scenario(
        positions, LLTV, PRICE, PRICE,
        total_borrow_assets=500_000_000, total_borrow_shares=500_000_000,
        market_shock=-0.3, oracle_shock_ratio=0.7, kappa=1, path_steps=3,
        slip_ab=(0.0, 1.0), gas_cost_loan_units=0,
    )
    assert result["n_executed"] == 0
    assert result["n_stranded"] == 1
