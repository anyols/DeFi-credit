import gzip
import json
from math import prod
from pathlib import Path

import pytest

from kvr import historical_replay as replay, spread_replay as spread
from reports.audit_spread_replay import reconcile


def test_spread_uses_lagged_aave_and_requires_coverage_and_net_carry():
    day = 31 * replay.DAY
    market = {t: {"supplyAssets": 10_000_000 * replay.UNIT, "borrowAssets": 8_000_000 * replay.UNIT,
                  "rateAtTarget": 25_000_000_000, "collateralAssetsUsd": 9_000_000}
              for t in range(replay.DAY, day, replay.DAY)}
    rates = {t: 0.01 for t in market}
    args = (day, {"M": market}, {"M": []}, rates, 1_000_000, 1_000_000, 7)
    assert spread.ranked_entries(*args, 50)[0][0] == "M"
    rates[day] = 100
    assert spread.ranked_entries(*args, 50)[0][0] == "M"
    assert spread.ranked_entries(*args, 10_000) == []
    market[day - replay.DAY]["collateralAssetsUsd"] = 7_000_000
    assert spread.ranked_entries(*args, 50) == []


def test_rotation_respects_aave_capacity_and_conserves_balance_after_fees():
    positions = {}
    cash, aave, amounts = spread.rotate(positions, 0, 10_000, {"M": {}}, 0, ["M"], 2_000, 50, 800)
    assert amounts == [800, 700]
    assert aave == 9_200 and cash == 0 and positions["M"] == 700
    assert cash + aave + sum(positions.values()) == 10_000 - 2 * 50


def test_aave_capacity_reduces_cash_without_aave_credit_loss():
    row = spread.terminal_value({}, 500, 1_500, {}, 0, 50, 1_000)
    assert row["cash_after_exit_usd"] == 1_450
    assert row["aave_residual_usd"] == 500
    assert row["reported_usd"] == 2_000
    assert row["equity_by_recovery_pct_usd"]["0"] == pytest.approx(1_950)


def test_no_qualifying_markets_reduces_to_aave_after_round_trip_gas():
    rates = {d: 0.04 for d in range(0, 3 * replay.DAY, replay.DAY)}
    row, path, decisions = spread.simulate({}, {}, {}, rates, 0, 3 * replay.DAY, 10_000, 7, 2_000, 50, float("inf"))
    expected = (10_000 - 50) * 1.04 ** (3 / 365) - 50
    assert row["cash_after_exit_usd"] == pytest.approx(expected)
    assert row["rebalance_transactions"] == 1
    assert all(not entry[2] for entry in decisions)
    assert all(point["aave_fraction"] == 1 for point in path)


def test_hold_hurdle_is_lower_than_entry_and_failed_gate_forces_exit():
    signals = {"held": {"gross_spread_usd": 50, "net_spread_usd": -150},
               "new": {"gross_spread_usd": 250, "net_spread_usd": 50},
               "weak": {"gross_spread_usd": 10, "net_spread_usd": -190}}
    assert spread.select_spread({"held": 1, "failed": 1}, signals) == ["held", "new"]


def test_written_off_position_must_pass_entry_hurdle_again():
    signals = {"written_off": {"gross_spread_usd": 50, "net_spread_usd": -150},
               "new": {"gross_spread_usd": 250, "net_spread_usd": 50}}
    assert spread.select_spread({"written_off": 0}, signals) == ["new"]
    signals["written_off"]["net_spread_usd"] = 25
    assert spread.select_spread({"written_off": 0}, signals) == ["new", "written_off"]


def test_rotation_cannot_sell_more_than_opening_morpho_liquidity():
    positions = {"M": 1000}
    markets = {"M": {0: {"supplyAssets": 10_000_000_000, "borrowAssets": 9_700_000_000}}}
    fills = {}
    cash, aave, _ = spread.rotate(positions, 0, 0, markets, 0, [], 1000, 50, float("inf"), fills=fills)
    assert fills == {"M": -300}
    assert positions == {"M": 700} and aave == 200 and cash == 0


def test_partial_fills_below_cost_hurdle_do_not_trigger_aave_withdrawal():
    positions = {}
    cash, aave, amounts = spread.rotate(
        positions, 0, 10_000, {"M": {}}, 0, ["M"], 2_000, 50, 800,
        minimum_fills={"M": 750},
    )
    assert positions == {} and amounts == [] and cash == 0 and aave == 10_000


def test_reference_audit_reconciles_published_cash_and_recovery_equity():
    data_dir = Path(__file__).resolve().parents[1] / "data" / "processed"
    with gzip.open(data_dir / "demo_replay.json.gz", "rt", encoding="utf-8") as source:
        inputs = json.load(source)
    saved = json.loads((data_dir / "spread_replay.json").read_text(encoding="utf-8"))
    audit = {}
    case = spread.run(inputs, audit=audit)
    assert case == saved["base"]
    capital = case["starting_capital_usd"]
    bridges = {name: reconcile(result, audit[name], capital)[0] for name, result in case["table"].items()}
    reference = bridges["LIQUIDITY_SCREEN"]
    bridge = bridges[spread.SPREAD]
    assert sum(bridge[key] - reference[key] for key in bridge) == pytest.approx(670_810.09, abs=0.01, rel=0)
    assert bridges["AAVE"]["gas_before_exit"] == bridges["AAVE"]["terminal_exit_gas"] == -50
    calendar = range(inputs["end"] - 183 * replay.DAY, inputs["end"], replay.DAY)
    growth = prod((1 + inputs["aave"][str(day)]) ** (1 / 365) for day in calendar)
    assert case["table"]["AAVE"]["cash_after_exit_usd"] == pytest.approx(
        (capital - 50) * growth - 50, abs=0.01, rel=0
    )
