"""Rebuild the strategy validation totals from the bundled replay inputs."""
from collections import defaultdict
import gzip
import json
from math import isclose
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from kvr import historical_replay as replay, spread_replay as spread

LIQUIDITY = "LIQUIDITY_SCREEN"


def reconcile(result, trace, capital):
    gas = trace["initial_gas"]
    markets = defaultdict(lambda: dict(deposited=0.0, withdrawn=0.0, accrued=0.0, bad_debt=0.0, gas=0.0))
    aave_gas = gas
    for row in trace["rebalances"]:
        if not isclose(row["nominal_before"] - row["nominal_after"], gas * row["actions"],
                       rel_tol=0, abs_tol=1e-6):
            raise ValueError(f"Cash/claim conservation failed on {replay.date(row['day'])}")
        aave_gas += gas * (row["actions"] - len(row["fills"]))
        for mid, amount in row["fills"].items():
            markets[mid]["deposited"] += max(0, amount)
            markets[mid]["withdrawn"] += max(0, -amount)
            markets[mid]["gas"] += gas
    for row in trace["days"]:
        for mid, (interest, loss) in row["morpho"].items():
            markets[mid]["accrued"] += interest
            markets[mid]["bad_debt"] += loss
    aave_exit = trace["settlements"]["AAVE"]
    aave_interest = sum(row["aave_interest"] for row in trace["days"])
    for mid, flow in markets.items():
        exit_leg = trace["settlements"].get(mid, {})
        flow["exit_withdrawn"] = exit_leg.get("withdrawn", 0.0)
        flow["exit_gas"] = exit_leg.get("gas", 0.0)
        flow["residual"] = exit_leg.get("residual", 0.0)
        flow["recovery_mark"] = exit_leg.get("covered", 0.0) / 2
        flow["pnl_at_50"] = (flow["withdrawn"] + flow["exit_withdrawn"] - flow["deposited"]
                             - flow["gas"] - flow["exit_gas"] + flow["recovery_mark"])
    bridge = {
        "aave_interest": aave_interest,
        "morpho_interest": sum(x["accrued"] for x in markets.values()),
        "recorded_bad_debt": -sum(x["bad_debt"] for x in markets.values()),
        "gas_before_exit": -aave_gas - sum(x["gas"] for x in markets.values()),
        "terminal_exit_gas": -aave_exit["gas"] - sum(x["exit_gas"] for x in markets.values()),
        "residual_face_not_withdrawn": -result["residual_claim_usd"],
        "residual_recovery_mark": (result["collateral_covered_claim_usd"] / 2 + aave_exit["residual"]),
    }
    cash = capital + sum(bridge.values()) - bridge["residual_recovery_mark"]
    equity = cash + bridge["residual_recovery_mark"]
    trade_pnl = aave_interest - aave_gas - aave_exit["gas"] + sum(x["pnl_at_50"] for x in markets.values())
    if not all(isclose(x, y, rel_tol=0, abs_tol=1e-5) for x, y in (
        (cash, result["cash_after_exit_usd"]), (equity, result["equity_by_recovery_pct_usd"]["50"]),
        (capital + trade_pnl, equity),
    )):
        raise ValueError("Cash, valuation and trade-level attribution do not reconcile")
    return bridge, markets


def audited_pair(data, floor):
    audit = {}
    result = spread.run(data, min_free=floor, include_paths=False, audit=audit)
    bridges, markets, targets = {}, {}, {}
    for name, trace in audit.items():
        bridges[name], markets[name] = reconcile(result["table"][name], trace, result["starting_capital_usd"])
        targets[name] = {row["day"]: set(row["targets"]) for row in trace["rebalances"]}
    return result, bridges, markets, targets, audit


def overlap(targets):
    advanced, basic = targets[spread.SPREAD], targets[LIQUIDITY]
    paired = [(left, basic[day]) for day, left in advanced.items()]
    intersection = sum(len(left & right) for left, right in paired)
    union = sum(len(left | right) for left, right in paired)
    return {"dates": len(paired), "identical_dates": sum(left == right for left, right in paired),
            "common_targets": intersection, "union_targets": union}


def market_differences(markets):
    advanced, basic = markets[spread.SPREAD], markets[LIQUIDITY]
    return {mid: advanced.get(mid, {}).get("pnl_at_50", 0) - basic.get(mid, {}).get("pnl_at_50", 0)
            for mid in sorted(advanced.keys() | basic.keys())}


def gap(result):
    table = result["table"]
    return table[spread.SPREAD]["equity_by_recovery_pct_usd"]["50"] - table[LIQUIDITY]["equity_by_recovery_pct_usd"]["50"]


def omit_market(data, market):
    return {**data, "histories": {mid: history for mid, history in data["histories"].items() if mid != market}}


def target_profile(market, data, targets, higher_targets, audit):
    profile = {}
    for name in (LIQUIDITY, spread.SPREAD):
        dates = [day for day, picks in targets[name].items() if market in picks]
        profile[name] = {
            "base_target_dates": [replay.date(day) for day in dates],
            "base_target_dates_below_4m": sum(
                replay.liquidity(data["histories"][market].get(str(day - replay.DAY))) < 4_000_000 for day in dates
            ),
            "higher_floor_target_count": sum(market in picks for picks in higher_targets[name].values()),
            "base_fills_usd": {replay.date(row["day"]): row["fills"][market]
                               for row in audit[name]["rebalances"] if market in row["fills"]},
        }
    return profile


def headline_rank_check(data):
    """Change only the ordering of new entrants that already cleared the spread gates."""
    original = spread.signal_rows

    def with_headline_apr(day, histories, repayments, aave, sleeve, min_free, cadence, gas):
        signals = original(day, histories, repayments, aave, sleeve, min_free, cadence, gas)
        for row in replay.candidates(day, histories, repayments, min_free):
            if row["market_id"] in signals:
                signals[row["market_id"]]["headline_apr"] = row["apr"]
        return signals

    def headline_order(signals):
        return sorted(((mid, row["headline_apr"]) for mid, row in signals.items() if row["net_spread_usd"] > 0),
                      key=lambda row: (-row[1], row[0]))

    with patch.object(spread, "signal_rows", with_headline_apr), patch.object(spread, "entry_order", headline_order):
        return spread.run(data, include_paths=False)["table"][spread.SPREAD]


def main():
    with gzip.open(ROOT / "data" / "processed" / "demo_replay.json.gz", "rt", encoding="utf-8") as source:
        data = json.load(source)
    base, bridge, markets, targets, audit = audited_pair(data, 2_000_000)
    stricter, strict_bridge, strict_markets, strict_targets, _ = audited_pair(data, 4_000_000)
    differences = market_differences(markets)
    strict_differences = market_differences(strict_markets)
    influential = sorted(differences, key=lambda mid: (-abs(differences[mid]), mid))[:5]
    no_market = {
        mid: gap(spread.run(omit_market(data, mid), include_paths=False))
        for mid in influential[:3]
    }
    cases = {
        "base": base, "higher_floor": stricter,
        "fortnightly": spread.run(data, cadence=14, include_paths=False),
        "smaller": spread.run(data, aum=1_000_000, include_paths=False),
        "aave_cap": spread.run(data, aave_capacity=1_000_000, include_paths=False),
    }
    print(json.dumps({
        "terminal": {case: {name: rows["table"][name] for name in (spread.SPREAD, LIQUIDITY, "AAVE")}
                     for case, rows in cases.items()},
        "bridge": bridge,
        "stricter_bridge": strict_bridge,
        "target_overlap": {"base": overlap(targets), "higher_floor": overlap(strict_targets)},
        "markets": [
            {"id": mid, "symbol": data["markets"][mid]["symbol"], "delta_at_50": differences[mid],
             "delta_at_50_higher_floor": strict_differences.get(mid, 0.0),
             "gap_if_excluded_from_both": no_market.get(mid),
             "flows": {name: markets[name].get(mid) for name in (LIQUIDITY, spread.SPREAD)},
             "targets": target_profile(mid, data, targets, strict_targets, audit)}
            for mid in influential
        ],
        "market_delta_sum": sum(differences.values()),
        "headline_order_only": headline_rank_check(data),
        "missing_held_rate_observations": {name: sum(day["missing_morpho_rates"] for day in trace["days"])
                                          for name, trace in audit.items()},
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
