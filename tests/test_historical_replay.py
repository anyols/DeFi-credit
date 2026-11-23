"""Regression checks for information timing and recoverable equity."""
import gzip
import json
import math
from pathlib import Path

import pytest

from kvr import historical_replay as replay


def snapshot(supply, borrow, rate=1_000_000_000):
    return {"supplyAssets": int(supply * replay.UNIT), "borrowAssets": int(borrow * replay.UNIT),
            "rateAtTarget": rate}


def test_rebalance_uses_lagged_supply_rates_and_repayments():
    day = 31 * replay.DAY
    supply = replay.MIN_SUPPLY * 2
    market = {t: snapshot(supply, supply) for t in range(replay.DAY, day, replay.DAY)}
    # A same-day Repay and a large change in same-day balances cannot enter the ranking.
    market[day] = snapshot(supply * 100, supply)
    late_entrant = {t: snapshot(supply / 4, supply / 8) for t in market}
    late_entrant[day] = market[day]
    ranked = replay.candidates(day, {"M": market, "L": late_entrant}, {"M": [day], "L": []})
    assert [row["market_id"] for row in ranked] == ["M"]
    assert not ranked[0]["recent_repay"]
    assert not ranked[0]["repay_screen_pass"]
    assert replay.targets(ranked, "GREEDY") == ["M"]
    assert replay.targets(ranked, "REPAY_SCREEN") == []
    ranked = replay.candidates(day, {"M": market}, {"M": [day - replay.DAY]})
    assert replay.targets(ranked, "REPAY_SCREEN") == ["M"]
    assert replay.targets(ranked, "LIQUIDITY_SCREEN") == []


def test_cash_exit_and_haircuts_only_apply_to_residual():
    market = {**snapshot(2_000, 1_700), "collateralAssetsUsd": 850}
    result = replay.valuation({"M": 1_000}, 100, {"M": {0: market}}, 0, 10)
    assert result["reported_usd"] == 1_100
    assert result["cash_after_exit_usd"] == 390
    assert result["residual_claim_usd"] == 700
    assert result["collateral_covered_claim_usd"] == 350
    assert result["equity_by_recovery_pct_usd"] == {"0": 390, "25": 477.5, "50": 565, "100": 740}


def test_replay_accrues_at_post_deposit_supply_rate_once_and_allocates_loss():
    positions = {"M": 1_000}
    rate = 1_000_000_000
    histories = {"M": {0: snapshot(9_000, 9_000, rate)}}
    replay.accrue(positions, histories, {"M": {0: 100}}, 0)
    interest = 1_000 * 0.9 * rate / 1e18 * replay.DAY
    assert positions["M"] == pytest.approx((1_000 + interest) * 0.99)


def test_complete_writeoff_leaves_no_claim_or_accrued_interest():
    positions = {"M": 1_000}
    histories = {"M": {0: snapshot(9_000, 9_000)}}
    replay.accrue(positions, histories, {"M": {0: 10_000}}, 0)
    assert positions["M"] == 0


def test_rebalance_conserves_assets_net_of_gas():
    positions = {"M": 1_000}
    histories = {"M": {0: snapshot(9_000, 8_700)}, "N": {0: snapshot(9_000, 6_000)}}
    cash, trades = replay.rebalance(positions, 0, histories, 0, ["N"], 10)
    assert cash >= 0
    assert cash + sum(positions.values()) == 1_000 - 10 * len(trades)


def test_bundled_replay_reproduces_terminal_values_and_aave_break_even():
    data = Path(__file__).resolve().parents[1] / "data" / "processed"
    with gzip.open(data / "demo_replay.json.gz", "rt", encoding="utf-8") as source:
        inputs = json.load(source)
    published = json.loads((data / "historical_replay.json").read_text(encoding="utf-8"))
    result = replay.run(inputs)
    gas = result["gas_usd_per_action"]
    assert gas == 50
    growth = math.prod(
        (1 + inputs["aave"][str(day)]) ** (1 / 365)
        for day in range(inputs["start"], inputs["end"], replay.DAY)
    )
    aave = (result["starting_capital_usd"] - gas) * growth - gas
    assert result["table"]["AAVE"]["cash_after_exit_usd"] == pytest.approx(aave, abs=1e-6)
    for case in replay.RECOVERY:
        key = str(case)
        morpho = max(result["table"][name]["equity_by_recovery_pct_usd"][key] for name in replay.STRATEGIES)
        assert result["aave_exit_fraction_to_match_best_morpho"][key] == pytest.approx(morpho / aave)
        assert published["aave_exit_fraction_to_match_best_morpho"][key] == pytest.approx(morpho / aave)
    for name, row in result["table"].items():
        assert row["equity_by_recovery_pct_usd"] == pytest.approx(
            published["table"][name]["equity_by_recovery_pct_usd"]
        )
