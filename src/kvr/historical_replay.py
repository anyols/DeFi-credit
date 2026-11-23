from bisect import bisect_left
from datetime import datetime, timezone

import config
from . import backtest, irm

DAY = backtest.DAY
UNIT = 1_000_000
MIN_SUPPLY = config.BACKTEST_MIN_SUPPLY_USD
SCREEN_DAYS = config.BACKTEST_ZOMBIE_LOOKBACK_DAYS
RATE_DAYS = 7
STRATEGIES = ("GREEDY", "REPAY_SCREEN", "LIQUIDITY_SCREEN")
RECOVERY = (0, 25, 50, 100)


def balances(record):
    return record["supplyAssets"] / UNIT, record["borrowAssets"] / UNIT


def usable(record):
    return record is not None and all(k in record for k in ("supplyAssets", "borrowAssets", "rateAtTarget"))


def liquidity(record):
    if record is None or "supplyAssets" not in record or "borrowAssets" not in record:
        return 0.0
    supply, borrow = balances(record)
    return max(0.0, supply - borrow)


def candidates(day, histories, repayments, min_free_usdc=None):
    if min_free_usdc is None:
        min_free_usdc = config.BACKTEST_AUM_USD / config.BACKTEST_TOP_K
    signal_day = day - DAY
    window_start = day - SCREEN_DAYS * DAY
    ranked = []
    for mid, history in histories.items():
        latest = history.get(signal_day)
        if not usable(latest):
            continue
        supply, borrow = balances(latest)
        if supply < MIN_SUPPLY or borrow <= 0:
            continue
        rates = []
        for t in range(day - RATE_DAYS * DAY, day, DAY):
            record = history.get(t)
            if not usable(record):
                break
            s, b = balances(record)
            if s <= 0:
                break
            rates.append(backtest.daily_rate_per_second(min(b / s, 1), record["rateAtTarget"]))
        if len(rates) != RATE_DAYS:
            continue
        observations = [history.get(t) for t in range(window_start, day, DAY)]
        utilization = [r["borrowAssets"] / r["supplyAssets"] for r in observations
                       if usable(r) and r["supplyAssets"] > 0]
        at_cap = sum(u >= config.BACKTEST_ZOMBIE_UTIL_THRESHOLD for u in utilization)
        paid = repayments[mid]
        recent_repay = bisect_left(paid, day) > bisect_left(paid, window_start)
        liquid = liquidity(latest)
        ranked.append({
            "market_id": mid, "apr": sum(rates) / RATE_DAYS * irm.SECONDS_PER_YEAR,
            "supply_usd": supply, "utilization": borrow / supply, "free_liquidity_usd": liquid,
            "indexed_collateral_usd": latest.get("collateralAssetsUsd"),
            "days_observed": len(utilization), "days_at_cap": at_cap, "recent_repay": recent_repay,
            "repay_screen_pass": len(utilization) == SCREEN_DAYS and (
                recent_repay or at_cap < config.BACKTEST_ZOMBIE_MIN_DAYS_AT_CAP),
            "liquidity_screen_pass": liquid >= min_free_usdc,
        })
    return sorted(ranked, key=lambda row: (-row["apr"], row["market_id"]))


def targets(ranked, strategy):
    eligible = [
        r["market_id"] for r in ranked
        if (strategy == "GREEDY" or r["repay_screen_pass"])
        and (strategy != "LIQUIDITY_SCREEN" or r["liquidity_screen_pass"])
    ]
    return eligible[:config.BACKTEST_TOP_K]


def withdraw(have, want, available, gas):
    amount = min(max(0.0, have - want), available)
    return amount if amount > gas else 0.0


def accrue(positions, histories, bad_debt, day, *, flows=None):
    missing = 0
    for mid, claim in positions.items():
        record = histories[mid].get(day)
        if usable(record):
            supply, borrow = balances(record)
            positions[mid] += backtest.income_with_dilution(claim, supply, borrow, record["rateAtTarget"])
        else:
            missing += 1
        accrued_claim = positions[mid]
        loss = bad_debt[mid].get(day, 0)
        if loss:
            if record is None or "supplyAssets" not in record:
                raise ValueError(f"Missing supply for loss allocation: {mid} at {day}")
            # Assumes the snapshot is pre-loss and the simulated claim is incremental.
            base = record["supplyAssets"] / UNIT + claim
            if base > 0:
                positions[mid] *= 1 - min(1, loss / base)
            else:
                positions[mid] = 0
        if flows is not None:
            flows[mid] = (accrued_claim - claim, accrued_claim - positions[mid])
    return missing


def rebalance(positions, cash, histories, day, selected, gas):
    # Use start-of-day execution capacity. Today's terms cannot change the target ranks.
    target = (cash + sum(positions.values())) / config.BACKTEST_TOP_K
    trades = {}
    for mid, have in list(positions.items()):
        amount = withdraw(have, target if mid in selected else 0,
                          liquidity(histories[mid].get(day)), gas)
        if amount:
            positions[mid] -= amount
            cash += amount - gas
            trades[mid] = -amount
        if positions[mid] <= 0:
            del positions[mid]
    for mid in selected:
        have = positions.get(mid, 0)
        amount = min(target - have, cash - gas)
        if amount > 1e-6:
            positions[mid] = have + amount
            cash -= amount + gas
            trades[mid] = amount
    return cash, trades


def valuation(positions, cash, histories, day, gas, *, settlements=None):
    proceeds, stranded = cash, 0.0
    residual = {}
    collateral_covered_claim = 0.0
    unpriced_claim = 0.0
    for mid, claim in positions.items():
        record = histories[mid].get(day, {})
        available = liquidity(record)
        amount = withdraw(claim, 0, available, gas)
        proceeds += amount - gas if amount else 0
        covered = 0.0
        if claim > amount:
            residual[mid] = claim - amount
            stranded += residual[mid]
            collateral = record.get("collateralAssetsUsd")
            borrow = record.get("borrowAssets", 0) / UNIT
            if collateral is None or borrow <= 0:
                unpriced_claim += residual[mid]
            else:
                covered = residual[mid] * min(1, max(0, collateral) / borrow)
                collateral_covered_claim += covered
        if settlements is not None:
            settlements[mid] = {
                "withdrawn": amount, "gas": gas if amount else 0.0,
                "residual": claim - amount, "covered": covered,
            }
    return {
        "reported_usd": cash + sum(positions.values()),
        "cash_after_exit_usd": proceeds, "residual_claim_usd": stranded,
        "residual_claim_by_market_usd": residual,
        "collateral_covered_claim_usd": collateral_covered_claim,
        "unpriced_residual_claim_usd": unpriced_claim,
        "equity_by_recovery_pct_usd": {str(p): proceeds + p / 100 * collateral_covered_claim for p in RECOVERY},
    }


def date(day):
    return datetime.fromtimestamp(day, timezone.utc).date().isoformat()


def recovery_equity(row, recovery_pct):
    return row["cash_after_exit_usd"] + recovery_pct / 100 * row["collateral_covered_claim_usd"]


def aave_exit_fraction_to_match_best_morpho(table, recovery_pct):
    """Fraction of post-cost Aave terminal value that ties the best Morpho rule."""
    best = max(recovery_equity(table[name], recovery_pct) for name in STRATEGIES)
    return best / table["AAVE"]["cash_after_exit_usd"]


def run(data, *, aum=None, rebalance_days=None, min_free_usdc=None, diagnostics=False):
    aum = config.BACKTEST_AUM_USD if aum is None else aum
    rebalance_days = config.BACKTEST_REBALANCE_DAYS if rebalance_days is None else rebalance_days
    min_free_usdc = aum / config.BACKTEST_TOP_K if min_free_usdc is None else min_free_usdc
    gas = config.BACKTEST_GAS_COST_USD
    if aum <= 2 * gas or rebalance_days < 1 or min_free_usdc < 0:
        raise ValueError("Capital must cover round-trip gas, cadence must be positive, and liquidity must be nonnegative")
    start, end = data["start"], data["end"]
    histories = {m: {int(d): r for d, r in h.items()} for m, h in data["histories"].items()}
    repayments = {m: pair[0] for m, pair in data["events"].items()}
    losses = {m: {int(d): v for d, v in pair[1].items()} for m, pair in data["events"].items()}
    decisions = {d: candidates(d, histories, repayments, min_free_usdc)
                 for d in range(start, end, rebalance_days * DAY)}
    table, audit = {}, {}
    for name in STRATEGIES:
        cash, positions = aum, {}
        missing = 0
        peak, drawdown = aum, 0.0
        audit[name] = {}
        for day in range(start, end, DAY):
            if day in decisions:
                selected = targets(decisions[day], name)
                cash, trades = rebalance(positions, cash, histories, day, selected, gas)
                audit[name][day] = {"selected": selected, "trades_usd": trades}
            missing += accrue(positions, histories, losses, day)
            if diagnostics:
                marked = valuation(positions, cash, histories, day + DAY, gas)["equity_by_recovery_pct_usd"]["50"]
                peak = max(peak, marked)
                drawdown = min(drawdown, marked / peak - 1)
        table[name] = valuation(positions, cash, histories, end, gas)
        table[name]["terminal_positions_usd"] = positions
        table[name]["held_market_days_without_rate"] = missing
        table[name]["rebalance_transactions"] = sum(len(v["trades_usd"]) for v in audit[name].values())
        if diagnostics:
            table[name]["gross_rebalance_turnover_usd"] = sum(
                abs(amount) for entry in audit[name].values() for amount in entry["trades_usd"].values()
            )
            table[name]["max_drawdown_at_50pct_recovery"] = drawdown
    aave = {int(d): r for d, r in data["aave"].items()}
    if any(day not in aave for day in range(start, end, DAY)):
        raise ValueError("Aave daily benchmark history is incomplete")
    bench = backtest.simulate_bench(aave, start, end - DAY, aum_usd=aum - gas)
    aave_equity = bench["equity_reported"][-1][1] - gas
    table["AAVE"] = valuation({}, aave_equity, {}, end, 0)
    if diagnostics:
        peak, drawdown = aum, 0.0
        for _, equity in bench["equity_reported"]:
            equity -= gas
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1)
        table["AAVE"].update(gross_rebalance_turnover_usd=aum - gas, rebalance_transactions=1,
                             max_drawdown_at_50pct_recovery=drawdown)
    for result in table.values():
        result["pnl_by_recovery_pct_usd"] = {
            p: equity - aum for p, equity in result["equity_by_recovery_pct_usd"].items()
        }
    examples = decision_examples(data["markets"], histories, decisions, audit, table, end)
    rankings = {str(p): sorted(table, key=lambda name: -table[name]["equity_by_recovery_pct_usd"][str(p)])
                for p in RECOVERY}
    aave_break_even = {
        str(p): aave_exit_fraction_to_match_best_morpho(table, p) for p in RECOVERY
    }
    return {"start": date(start), "terminal_exit": date(end), "snapshot_block": data["snapshot_block"],
            "starting_capital_usd": aum, "min_free_usdc": min_free_usdc,
            "rebalance_days": rebalance_days, "top_k": config.BACKTEST_TOP_K,
            "minimum_lagged_supply_usd": MIN_SUPPLY, "gas_usd_per_action": gas,
            "markets_discovered": data["discovered"], "markets_with_qualifying_history": len(histories),
            "markets_ever_rankable": len({r["market_id"] for rows in decisions.values() for r in rows}),
            "table": table, "rankings_by_recovery_pct": rankings,
            "aave_exit_fraction_to_match_best_morpho": aave_break_even, "worked_decisions": examples}


def decision_examples(markets, histories, decisions, audit, table, end):
    examples = {}
    for day, rows in decisions.items():
        for row in rows:
            mid = row["market_id"]
            trades = {name: audit[name][day]["trades_usd"].get(mid, 0) for name in STRATEGIES}
            if trades["GREEDY"] <= 0 or mid in audit["LIQUIDITY_SCREEN"][day]["selected"]:
                continue
            later = sorted((d, r) for d, r in histories[mid].items() if day < d <= end and usable(r))
            first_cap = next((d for d, r in later if r["supplyAssets"] > 0
                              and r["borrowAssets"] / r["supplyAssets"] >= config.BACKTEST_ZOMBIE_UTIL_THRESHOLD), None)
            if first_cap is None:
                continue
            term_liq = liquidity(histories[mid].get(end))
            scenario = "already_illiquid" if row["days_at_cap"] else "before_deterioration"
            if scenario in examples or term_liq >= trades["GREEDY"]:
                continue
            future_cap_days = sum(
                r["supplyAssets"] > 0 and r["borrowAssets"] / r["supplyAssets"] >= config.BACKTEST_ZOMBIE_UTIL_THRESHOLD
                for d, r in later if first_cap <= d < first_cap + SCREEN_DAYS * DAY
            )
            if future_cap_days < config.BACKTEST_ZOMBIE_MIN_DAYS_AT_CAP:
                continue
            examples[scenario] = {
                **row, "symbol": markets[mid]["symbol"], "decision_date": date(day),
                "latest_input_date": date(day - DAY), "next_99pct_utilization_date": date(first_cap),
                "net_trade_usd": trades, "terminal_market_free_liquidity_usd": term_liq,
                "selected_markets": {
                    name: [{"market_id": m, "symbol": markets[m]["symbol"]} for m in audit[name][day]["selected"]]
                    for name in STRATEGIES
                },
                "subsequent_rebalance_withdrawals_usd": {
                    name: sum(-min(0, v["trades_usd"].get(mid, 0))
                              for d, v in audit[name].items() if d > day)
                    for name in STRATEGIES
                },
                "terminal_residual_claim_usd": {
                    name: table[name]["residual_claim_by_market_usd"].get(mid, 0) for name in STRATEGIES
                },
                "days_at_cap_in_first_30_days_after_crossing": future_cap_days,
            }
    return examples
