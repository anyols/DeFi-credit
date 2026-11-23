"""Retrospective evaluation of an Aave-funded, liquidity-gated carry rule."""
from math import isfinite

import config
from . import backtest, historical_replay as replay

DAY = replay.DAY
EVALUATION_DAYS = 183
SPREAD = "ADJUSTED_SPREAD"


def signal_rows(day, histories, repayments, aave, sleeve, min_free, cadence, gas):
    """Estimate excess carry exclusively from the seven completed daily observations."""
    observed = [aave.get(t) for t in range(day - replay.RATE_DAYS * DAY, day, DAY)]
    if any(rate is None for rate in observed):
        return {}
    aave_daily = sum((1 + rate) ** (1 / 365) - 1 for rate in observed) / len(observed)
    signals = {}
    for row in replay.candidates(day, histories, repayments, min_free):
        mid = row["market_id"]
        if not row["repay_screen_pass"] or not row["liquidity_screen_pass"]:
            continue
        if row["free_liquidity_usd"] < sleeve:
            continue
        _, debt = replay.balances(histories[mid][day - DAY])
        collateral = row["indexed_collateral_usd"]
        if collateral is None or not isfinite(collateral) or collateral < debt:
            continue
        daily_income = 0.0
        for t in range(day - replay.RATE_DAYS * DAY, day, DAY):
            rec = histories[mid][t]
            supply, borrow = replay.balances(rec)
            daily_income += backtest.income_with_dilution(sleeve, supply, borrow, rec["rateAtTarget"])
        daily_income /= replay.RATE_DAYS
        gross = cadence * (daily_income - sleeve * aave_daily)
        signals[mid] = {
            "gross_spread_usd": gross, "net_spread_usd": gross - 4 * gas,
            "annual_yield_advantage": (daily_income / sleeve - aave_daily) * 365,
            "lagged_free_usdc": row["free_liquidity_usd"],
            "collateral_to_debt": collateral / debt, "recent_repay": row["recent_repay"],
        }
    return signals


def ranked_entries(day, histories, repayments, aave, sleeve, min_free, cadence, gas):
    signals = signal_rows(day, histories, repayments, aave, sleeve, min_free, cadence, gas)
    return entry_order(signals)[:config.BACKTEST_TOP_K]


def entry_order(signals):
    return sorted(((mid, row["net_spread_usd"]) for mid, row in signals.items()
                   if row["net_spread_usd"] > 0), key=lambda row: (-row[1], row[0]))


def select_spread(positions, signals):
    # Keep an eligible incumbent once the entry costs are sunk.
    holds = sorted(mid for mid, claim in positions.items()
                   if claim > 0 and signals.get(mid, {}).get("gross_spread_usd", 0) > 0)
    entries = [mid for mid, _ in entry_order(signals) if mid not in holds]
    return (holds + entries)[:config.BACKTEST_TOP_K]


def rotate(positions, cash, aave_claim, histories, day, selected, sleeve, gas, aave_capacity,
           *, funding_targets=None, minimum_fills=None, fills=None):
    """Rebalance fixed-size sleeves and sweep remaining cash to Aave once per rebalance."""
    amounts = []
    for mid, claim in list(positions.items()):
        want = sleeve if mid in selected else 0
        amount = replay.withdraw(claim, want, replay.liquidity(histories[mid].get(day)), gas)
        if amount:
            positions[mid] -= amount
            cash += amount - gas
            amounts.append(amount)
            if fills is not None:
                fills[mid] = -amount
        if positions[mid] <= 0:
            del positions[mid]

    needs = [(mid, (sleeve - positions.get(mid, 0)) if funding_targets is None
              else funding_targets.get(mid, 0)) for mid in selected]
    budget = cash + max(0, min(aave_claim, aave_capacity) - gas)
    planned = []
    for mid, requested in needs:
        amount = min(requested, budget - gas)
        minimum = (minimum_fills or {}).get(mid, gas)
        if amount > max(gas, minimum):
            planned.append((mid, amount))
            budget -= amount + gas
    needs = planned
    required = sum(amount + gas for _, amount in needs)
    draw = min(max(0, required + gas - cash), aave_claim, aave_capacity) if required > cash else 0
    if draw > gas:
        aave_claim -= draw
        cash += draw - gas
        amounts.append(draw)

    for mid, requested in needs:
        amount = min(requested, cash - gas)
        if amount > gas:
            positions[mid] = positions.get(mid, 0) + amount
            cash -= amount + gas
            amounts.append(amount)
            if fills is not None:
                fills[mid] = amount
    if cash > gas:
        amounts.append(cash - gas)
        aave_claim += cash - gas
        cash = 0.0
    return cash, aave_claim, amounts


def terminal_value(positions, cash, aave_claim, histories, day, gas, aave_capacity, *, settlements=None):
    draw = replay.withdraw(aave_claim, 0, aave_capacity, gas)
    row = replay.valuation(positions, cash + (draw - gas if draw else 0), histories, day, gas,
                           settlements=settlements)
    remainder = aave_claim - draw
    row["reported_usd"] += aave_claim - (draw - gas if draw else 0)
    row["aave_residual_usd"] = remainder
    row["residual_claim_usd"] += remainder
    if settlements is not None:
        settlements["AAVE"] = {"withdrawn": draw, "gas": gas if draw else 0.0, "residual": remainder}
    for case in replay.RECOVERY:
        row["equity_by_recovery_pct_usd"][str(case)] += remainder
    return row


def rebalance_plan(name, positions, day, histories, repayments, aave, sleeve, min_free, cadence, gas):
    if name == "AAVE":
        return [], {}, {}
    if name == SPREAD:
        signals = signal_rows(day, histories, repayments, aave, sleeve, min_free, cadence, gas)
        selected = select_spread(positions, signals)
    else:
        signals = {}
        eligible = [r for r in replay.candidates(day, histories, repayments, min_free)
                    if r["free_liquidity_usd"] > gas]
        selected = replay.targets(eligible, name)
    funding = {}
    for mid in selected:
        # Opening free USDC is a conservative sleeve-capacity proxy, not a protocol deposit limit.
        gap = min(sleeve - positions.get(mid, 0), replay.liquidity(histories[mid].get(day)))
        # Scale the expected gas hurdle to the intended fill. Insufficient funded cash can
        # further reduce fills under the Aave capacity stress.
        if name != SPREAD or signals[mid]["gross_spread_usd"] * gap / sleeve > 4 * gas:
            funding[mid] = gap
    return selected, funding, signals


def simulate(histories, repayments, losses, aave, start, end, aum, cadence, min_free, gas, capacity,
             *, name=SPREAD, audit=None):
    cash, aave_claim, positions, amounts = 0.0, aum - gas, {}, [aum - gas]
    sleeve = aum / config.BACKTEST_TOP_K
    peak, drawdown = aum, 0.0
    path, decisions, wanted = [], [], []
    if audit is not None:
        audit.update(initial_gas=gas, rebalances=[], days=[], settlements={})
    for day in range(start, end, DAY):
        if (day - start) % (cadence * DAY) == 0:
            before = {mid: claim for mid, claim in positions.items() if claim > 0}
            if audit is not None:
                nominal_before = cash + aave_claim + sum(positions.values())
            wanted, funding, signals = rebalance_plan(
                name, positions, day, histories, repayments, aave, sleeve, min_free, cadence, gas
            )
            fills = {}
            minimum_fills = {mid: 4 * gas * sleeve / signals[mid]["gross_spread_usd"]
                             for mid in funding if mid in signals}
            cash, aave_claim, trades = rotate(
                positions, cash, aave_claim, histories, day, wanted, sleeve, gas, capacity,
                funding_targets=funding, minimum_fills=minimum_fills, fills=fills,
            )
            amounts.extend(trades)
            if name == SPREAD:
                decisions.append((day, before, wanted, signals, fills, dict(positions)))
            if audit is not None:
                audit["rebalances"].append({
                    "day": day, "targets": wanted, "fills": fills, "actions": len(trades),
                    "nominal_before": nominal_before, "nominal_after": cash + aave_claim + sum(positions.values()),
                })
        flows = {} if audit is not None else None
        missing = replay.accrue(positions, histories, losses, day, flows=flows)
        aave_before = aave_claim
        aave_claim *= (1 + aave[day]) ** (1 / 365)
        if audit is not None:
            audit["days"].append({
                "day": day, "morpho": flows, "aave_interest": aave_claim - aave_before,
                "missing_morpho_rates": missing,
            })
        marked = terminal_value(positions, cash, aave_claim, histories, day + DAY, gas, capacity)
        equity = marked["equity_by_recovery_pct_usd"]["50"]
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
        trapped = sum(max(0, claim - (sleeve if mid in wanted else 0))
                      for mid, claim in positions.items())
        distressed = 0.0
        for mid, claim in positions.items():
            record = histories[mid].get(day)
            if replay.usable(record) and record["supplyAssets"] > 0 and (
                record["borrowAssets"] / record["supplyAssets"] >= config.BACKTEST_ZOMBIE_UTIL_THRESHOLD
            ):
                distressed += claim
        # Share denominator stays in nominal units. The performance curve uses recovery marks.
        nominal = marked["reported_usd"]
        path.append({"date": replay.date(day + DAY), "marked_equity_usd": equity,
                     "aave_fraction": aave_claim / nominal if nominal else 0,
                     "morpho_fraction": sum(positions.values()) / nominal if nominal else 0,
                     "exit_requested_claim_usd": trapped,
                     "distressed_claim_usd": distressed,
                     "exit_requested_fraction": trapped / nominal if nominal else 0})
    row = terminal_value(positions, cash, aave_claim, histories, end, gas, capacity,
                         settlements=audit["settlements"] if audit is not None else None)
    row.update(gross_rebalance_turnover_usd=sum(amounts), rebalance_transactions=len(amounts),
               max_drawdown_at_50pct_recovery=drawdown,
               mean_aave_fraction=sum(p["aave_fraction"] for p in path) / len(path),
               mean_morpho_fraction=sum(p["morpho_fraction"] for p in path) / len(path),
               days_with_exit_requested_claim=sum(p["exit_requested_claim_usd"] > 1 for p in path),
               days_with_distressed_exposure=sum(p["distressed_claim_usd"] > 1 for p in path),
               annualized_at_50pct_recovery=(row["equity_by_recovery_pct_usd"]["50"] / aum) **
               (365 * DAY / (end - start)) - 1)
    return row, path, decisions


def worked_decisions(decisions, markets, histories, repayments, min_free, terminal):
    """First executed entry, first economic hold and first blocked exit in replay order."""
    found = {}
    for i, (day, before, wanted, signals, fills, after) in enumerate(decisions):
        for mid in sorted(set(before) | set(fills)):
            fill = fills.get(mid, 0)
            signal = signals.get(mid)
            action = None
            if fill > 0 and mid not in before:
                action = "entry"
            elif mid in before and mid in wanted and fill == 0:
                action = "hold"
            elif mid in before and mid not in wanted and after.get(mid, 0) > 1:
                action = "blocked_exit"
            if action is None or action in found:
                continue
            later_withdrawals = sum(-min(0, entry[4].get(mid, 0)) for entry in decisions[i+1:])
            lagged = replay.candidates(day, {mid: histories[mid]}, repayments, min_free)
            observed = lagged[0] if lagged else None
            found[action] = {"date": replay.date(day), "market_id": mid,
                             "symbol": markets[mid]["symbol"], "signal": signal, "observed": observed,
                             "before_usdc": before.get(mid, 0), "trade_usdc": fill,
                             "after_usdc": after.get(mid, 0), "later_withdrawals_usdc": later_withdrawals,
                             "terminal_residual_usdc": terminal["residual_claim_by_market_usd"].get(mid, 0)}
    return found


def run(data, *, aum=None, cadence=None, min_free=None, aave_capacity=None, include_paths=True, audit=None):
    """Compare on the final 183 days with fixed rules and identical execution conventions."""
    start, end = data["end"] - EVALUATION_DAYS * DAY, data["end"]
    aum = config.BACKTEST_AUM_USD if aum is None else aum
    cadence = config.BACKTEST_REBALANCE_DAYS if cadence is None else cadence
    min_free = aum / config.BACKTEST_TOP_K if min_free is None else min_free
    gas = config.BACKTEST_GAS_COST_USD
    if start <= data["start"] or (aave_capacity is not None and aave_capacity < 0):
        raise ValueError("Need earlier evaluation history and nonnegative Aave capacity")
    if aum <= 2 * gas or cadence < 1 or min_free < 0:
        raise ValueError("Need capital above round-trip gas, positive cadence and nonnegative liquidity")
    aave = {int(day): rate for day, rate in data["aave"].items()}
    if any(day not in aave for day in range(start, end, DAY)):
        raise ValueError("Aave daily benchmark history is incomplete")
    histories = {m: {int(d): r for d, r in h.items()} for m, h in data["histories"].items()}
    repayments = {m: pair[0] for m, pair in data["events"].items()}
    losses = {m: {int(d): v for d, v in pair[1].items()} for m, pair in data["events"].items()}
    capacity = float("inf") if aave_capacity is None else aave_capacity
    table, paths, examples = {}, {}, {}
    for name in (*replay.STRATEGIES, "AAVE", SPREAD):
        row, path, decisions = simulate(histories, repayments, losses, aave, start, end,
                                        aum, cadence, min_free, gas, capacity, name=name,
                                        audit=audit.setdefault(name, {}) if audit is not None else None)
        table[name] = row
        if include_paths:
            paths[name] = path
            if name == SPREAD:
                examples = worked_decisions(decisions, data["markets"], histories, repayments, min_free, row)
    return {"start": replay.date(start), "terminal_exit": replay.date(end),
            "starting_capital_usd": aum, "rebalance_days": cadence, "min_free_usdc": min_free,
            "aave_withdrawal_cap_usd": aave_capacity, "table": table,
            "paths": paths, "worked_decisions": examples}
