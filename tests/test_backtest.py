"""Task 6: validation tests for backtest.py -- a toy 2-market backtest with hand-computed P&L,
a liquidity-trapped withdrawal case, and REL_VALUE's switch-threshold logic. Chain-free and
deterministic, like T7's toy cases: no real history/API calls, hand-built dicts only.
"""

import pytest

from kvr import backtest
import config


def test_toy_2market_backtest_hand_computed_pnl():
    # Two markets, u=0.9 exactly at both -- at u==TARGET_UTILIZATION, AdaptiveCurveIrm's own
    # curve collapses to borrow_rate_per_second == rate_at_target exactly (hand-derived from
    # irm.curve: err=0 at u=target, so inner=WAD and the result is just rate_at_target itself),
    # which is what makes this case hand-verifiable without re-deriving the whole IRM curve.
    # Market A: $9M rest-of-market supply/borrow, my $1M -> new_supply=$10M, u=9M/10M=0.9.
    # Market B: $4.5M rest-of-market, my $0.5M -> new_supply=$5M, u=4.5M/5M=0.9.
    rec_a = {"utilization": 0.9, "rateAtTarget": 31709792, "supplyAssetsUsd": 9_000_000, "borrowAssetsUsd": 9_000_000, "collateralAssetsUsd": 9_000_000}
    rec_b = {"utilization": 0.9, "rateAtTarget": 15854896, "supplyAssetsUsd": 4_500_000, "borrowAssetsUsd": 4_500_000, "collateralAssetsUsd": 4_500_000}
    universe = [
        {"market_id": "A", "symbol": "A", "lltv": int(0.8 * 10**18), "oracle_address": "0xA", "collateral_token": "0xA", "loan_token": "0xU"},
        {"market_id": "B", "symbol": "B", "lltv": int(0.8 * 10**18), "oracle_address": "0xB", "collateral_token": "0xB", "loan_token": "0xU"},
    ]
    histories = {"A": {0: rec_a, 86400: rec_a}, "B": {0: rec_b, 86400: rec_b}}

    def fixed_split(day, universe, histories, positions, state):
        return {"A": 1_000_000 / 1_500_000, "B": 500_000 / 1_500_000}, state

    result = backtest.simulate(
        universe, histories, bad_debt_by_market={}, strategy_fn=fixed_split,
        window_start_day=0, window_end_day=86400, aum_usd=1_500_000, rebalance_days=1_000_000, gas_usd=0.0,
    )

    # Day 0: pure deployment, no income yet (nothing was held entering day 0).
    assert result["equity_reported"][0] == (0, pytest.approx(1_500_000.0))

    # Day 1: one day of income with own-deposit dilution, hand-verified via
    # income_with_dilution's own independently-derivable formula (see its docstring):
    # The supply rate already includes utilization. Apply it directly to the lender balance.
    expected_income_a = 1_000_000 * (31709792 / 10**18 * 0.9) * 86400
    expected_income_b = 500_000 * (15854896 / 10**18 * 0.9) * 86400
    expected_day1 = 1_500_000 + expected_income_a + expected_income_b
    assert result["equity_reported"][1] == pytest.approx((86400, expected_day1))
    assert result["equity_marked"][1] == pytest.approx((86400, expected_day1))  # neither market is a zombie here
    assert result["n_switches"] == 2  # one enter for A, one for B, on day 0 only


def test_trapped_withdrawal_when_liquidity_is_insufficient():
    # Day 0: deploy $1,000,000 into M (rate_at_target=0 everywhere -- zero income, isolating
    # the withdrawal mechanic from any accrual math). Day 1: the strategy wants out entirely,
    # but the market only has $100,000 of available liquidity (supply-borrow) -- $100,000 comes
    # back as cash, $900,000 stays behind as a tracked, explicitly "trapped" position.
    rec_day0 = {"utilization": 0.5, "rateAtTarget": 0, "supplyAssetsUsd": 2_000_000, "borrowAssetsUsd": 1_000_000, "collateralAssetsUsd": 2_000_000}
    rec_day1 = {"utilization": 0.5, "rateAtTarget": 0, "supplyAssetsUsd": 1_100_000, "borrowAssetsUsd": 1_000_000, "collateralAssetsUsd": 1_100_000}
    universe = [{"market_id": "M", "symbol": "M", "lltv": int(0.8 * 10**18), "oracle_address": "0xM", "collateral_token": "0xM", "loan_token": "0xU"}]
    histories = {"M": {0: rec_day0, 86400: rec_day1}}

    calls = {"n": 0}

    def flip_then_exit(day, universe, histories, positions, state):
        calls["n"] += 1
        return ({"M": 1.0}, state) if calls["n"] == 1 else ({}, state)

    result = backtest.simulate(
        universe, histories, bad_debt_by_market={}, strategy_fn=flip_then_exit,
        window_start_day=0, window_end_day=86400, aum_usd=1_000_000, rebalance_days=1, gas_usd=0.0,
    )

    assert result["final_positions"] == {"M": pytest.approx(900_000.0)}
    assert result["final_cash"] == pytest.approx(100_000.0)
    assert result["equity_reported"][-1][1] == pytest.approx(1_000_000.0)  # conserved: no income, no gas, no bad debt
    assert result["trapped_usd_series"][-1] == (86400, pytest.approx(900_000.0))


def test_rel_value_does_not_switch_below_threshold_but_does_above_it():
    # Both markets at u=0.9 (so the IRM's own curve collapse applies, see the toy-backtest test
    # above) -- "held" at ~5%/yr, a challenger at either +10bps or +50bps, with
    # config.BACKTEST_SWITCH_THRESHOLD_BPS=25 and a negligible gas-bps-equivalent at this AUM
    # (computed by hand: config.BACKTEST_GAS_COST_USD / config.BACKTEST_AUM_USD * 10_000 ~= 0.05bps).
    held_rat = 1_761_517_973  # ~5.00%/yr at u=0.9 (verified by hand via trailing_avg_apr)
    small_gap_rat = held_rat + int(0.001 * 10**18 / (0.9 * 31536000))  # +10bps
    big_gap_rat = held_rat + int(0.005 * 10**18 / (0.9 * 31536000))  # +50bps

    def hist_const(rate_at_target):
        rec = {"utilization": 0.9, "rateAtTarget": rate_at_target, "supplyAssetsUsd": 9_000_000, "borrowAssetsUsd": 9_000_000, "collateralAssetsUsd": 9_000_000}
        return {i * 86400: rec for i in range(7)}

    end_day = 6 * 86400
    universe = [
        {"market_id": "held", "symbol": "X", "lltv": int(0.8 * 10**18), "oracle_address": "0xH"},
        {"market_id": "challenger", "symbol": "X", "lltv": int(0.8 * 10**18), "oracle_address": "0xC"},
    ]

    bucket = backtest.bucket_key("X", "MARKET", 0.8)

    def run(challenger_rat):
        histories = {"held": hist_const(held_rat), "challenger": hist_const(challenger_rat)}
        # Pre-seed "held" as the CURRENT holding (not the initial pick) -- this test is about
        # the switch decision, not the first-ever selection, which never consults the threshold.
        state = {
            "oracle_class": {"held": "MARKET", "challenger": "MARKET"},
            "has_dex": {"held": True, "challenger": True},
            "current_holding_per_bucket": {bucket: "held"},
        }
        weights, state = backtest.strategy_rel_value(end_day, universe, histories, {}, state)
        return weights, state

    weights_small, _ = run(small_gap_rat)
    assert weights_small == {"held": 1.0}  # +10bps < 25bps threshold -- no switch

    weights_big, state_big = run(big_gap_rat)
    assert weights_big == {"challenger": 1.0}  # +50bps > 25bps threshold -- switches
    assert state_big["gap_log"][-1]["net_gap_bps"] == pytest.approx(49.95, abs=0.1)
