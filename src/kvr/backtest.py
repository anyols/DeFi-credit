"""Shared rate math and the exploratory Ethereum Morpho USDC comparison.

The exploratory universe uses current supply and its static exit filters use the
snapshot or live quotes. Use historical_replay for the lagged strategy comparison.

Rates are read from the on-chain IRM (historical rateAtTarget + utilization -> irm.borrow_rate),
never the API's own APY fields, matching this project's rule everywhere else.
"""

import datetime as dt

import requests

import config
from . import api, chain, dex, irm, oracle, spread
from . import morpho_math as mm

ZERO = "0x" + "00" * 20
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAY = 86400


def _day(ts):
    """Normalize a unix timestamp to its UTC calendar-day bucket (midnight)."""
    return int(ts) // DAY * DAY


def discover_universe(min_supply_usd=None):
    """Every Ethereum Morpho USDC market with CURRENT supplyAssetsUsd >= min_supply_usd -- see
    config.BACKTEST_MIN_SUPPLY_USD's own docstring for why this (not a true all-time scan) is
    the universe, and the survivorship-bias risk it leaves. Reuses spread.usdc_market_universe
    (already includes creationBlockNumber).
    """
    min_supply_usd = min_supply_usd if min_supply_usd is not None else config.BACKTEST_MIN_SUPPLY_USD
    raw = spread.usdc_market_universe(min_supply_usd=min_supply_usd, first=300)
    markets = []
    for m in raw:
        if not m.get("collateralAsset") or not m.get("oracle"):
            continue  # idle market (no collateral/oracle) -- can't be borrowed against, not a candidate
        markets.append(
            {
                "market_id": m["marketId"],
                "symbol": m["collateralAsset"]["symbol"],
                "lltv": int(m["lltv"]),
                "oracle_address": m["oracle"]["address"],
                "collateral_token": m["collateralAsset"]["address"],
                "loan_token": m["loanAsset"]["address"],
                "creation_block": int(m["creationBlockNumber"]),
            }
        )
    return markets


def pull_daily_history(market_id, window_start_ts, window_end_ts, chain_id=None):
    """Daily supplyAssetsUsd/borrowAssetsUsd/collateralAssetsUsd/utilization/rateAtTarget for
    one market over the window, as {day_bucket: {field: value}}. A market created after
    window_start simply has fewer days -- not an error, handled by every caller treating a
    missing day as "this position/market didn't exist yet"."""
    chain_id = chain_id if chain_id is not None else config.CHAIN_ID
    q = """
    query($id: String!, $chainId: Int!, $start: Int, $end: Int) {
      marketById(marketId: $id, chainId: $chainId) {
        historicalState {
          supplyAssetsUsd(options: { startTimestamp: $start, endTimestamp: $end, interval: DAY }) { x y }
          borrowAssetsUsd(options: { startTimestamp: $start, endTimestamp: $end, interval: DAY }) { x y }
          collateralAssetsUsd(options: { startTimestamp: $start, endTimestamp: $end, interval: DAY }) { x y }
          utilization(options: { startTimestamp: $start, endTimestamp: $end, interval: DAY }) { x y }
          rateAtTarget(options: { startTimestamp: $start, endTimestamp: $end, interval: DAY }) { x y }
        }
      }
    }
    """
    data = api.graphql(q, {"id": market_id, "chainId": chain_id, "start": window_start_ts, "end": window_end_ts})
    hs = data["marketById"]["historicalState"]
    by_day = {}
    for field in ("supplyAssetsUsd", "borrowAssetsUsd", "collateralAssetsUsd", "utilization", "rateAtTarget"):
        for p in hs[field]:
            if p["y"] is None:
                continue
            by_day.setdefault(_day(p["x"]), {})[field] = p["y"]
    return by_day


def daily_rate_per_second(utilization_float, rate_at_target_raw, fee_frac=0.0):
    """Supply rate per second (float), from irm.borrow_rate -- the on-chain IRM curve, not an
    API APY field. utilization_float: plain 0-1 fraction (the historical API's own units,
    verified by hand against an on-chain read). rate_at_target_raw: the raw per-second WAD
    integer (verified by hand: the historical API field is the SAME units as the on-chain
    rateAtTarget() call, not a human-readable annualized number)."""
    if utilization_float is None or rate_at_target_raw is None:
        return None
    u_wad = int(min(max(utilization_float, 0.0), 1.0) * mm.WAD)
    borrow_rate_per_second, _end_rt = irm.borrow_rate(u_wad, int(rate_at_target_raw), 0)
    return borrow_rate_per_second / mm.WAD * utilization_float * (1 - fee_frac)


def trailing_avg_apr(history, end_day, days, fee_frac=0.0):
    """Average of the trailing `days` days' own daily supply rate, annualized via APR (never
    APY -- see irm.apy_from_per_second_rate's own docstring for why that blows up on a
    pinned/zombie rate). Only uses data up to and including end_day -- point-in-time, no
    look-ahead. None if no day in the window has usable data (market didn't exist yet, etc.)."""
    rates = []
    for day in range(end_day - (days - 1) * DAY, end_day + 1, DAY):
        rec = history.get(day)
        if not rec:
            continue
        rate_ps = daily_rate_per_second(rec.get("utilization"), rec.get("rateAtTarget"), fee_frac)
        # rate_ps is already a plain per-second fraction (daily_rate_per_second's own WAD
        # division), NOT a raw WAD-scaled rate -- irm.apr_from_per_second_rate expects the
        # latter and would divide by WAD a second time. Annualize directly instead.
        if rate_ps is not None:
            rates.append(rate_ps * irm.SECONDS_PER_YEAR)
    return sum(rates) / len(rates) if rates else None


def income_with_dilution(x_usd, supply_usd, borrow_usd, rate_at_target_raw, fee_frac=0.0):
    """One day's USD income from holding x_usd in a market with supply_usd/borrow_usd (the REST
    of the market, i.e. the real-world API state, excluding this hypothetical position) --
    includes the OWN-DEPOSIT rate impact: diluting utilization down as x_usd is added, which
    the IRM responds to, same structure as optimize.income_rate but per day, not annualized via
    APY (same "measured rates, no blowup" reasoning as trailing_avg_apr). Assumes borrow demand
    (borrow_usd) is unaffected by the deposit -- optimize.py's own M11 "hold Bor_i fixed"
    assumption, inherited here, and the same one that breaks down for an already-pinned market
    (see optimize.slsqp_check's docstring) -- labeled, not hidden.
    """
    if x_usd <= 0 or rate_at_target_raw is None or borrow_usd is None or borrow_usd <= 0:
        return 0.0
    new_supply = supply_usd + x_usd
    if new_supply <= 0:
        return 0.0
    u = min(borrow_usd / new_supply, 1.0)
    rate_ps = daily_rate_per_second(u, rate_at_target_raw, fee_frac)
    if rate_ps is None:
        return 0.0
    return x_usd * rate_ps * DAY


def is_zombie_asof(history, end_day, util_threshold=None, lookback_days=None, min_days_at_cap=None):
    """Point-in-time rolling zombie flag: >=util_threshold utilization for >=min_days_at_cap of
    the trailing lookback_days days, using only data up to and including end_day -- distinct
    from spread.classify_zombie (tuned for "is this stuck right now", not "would a strategy
    running on this specific past date have excluded it")."""
    util_threshold = util_threshold if util_threshold is not None else config.BACKTEST_ZOMBIE_UTIL_THRESHOLD
    lookback_days = lookback_days if lookback_days is not None else config.BACKTEST_ZOMBIE_LOOKBACK_DAYS
    min_days_at_cap = min_days_at_cap if min_days_at_cap is not None else config.BACKTEST_ZOMBIE_MIN_DAYS_AT_CAP
    days_at_cap = 0
    for day in range(end_day - (lookback_days - 1) * DAY, end_day + 1, DAY):
        rec = history.get(day)
        if rec and rec.get("utilization") is not None and rec["utilization"] >= util_threshold:
            days_at_cap += 1
    return days_at_cap >= min_days_at_cap


def bucket_key(symbol, oracle_class, lltv_frac, tolerance=None):
    """REL_VALUE's "same-risk bucket": same collateral, same oracle class, LLTV within
    `tolerance` (default config.BACKTEST_LLTV_BUCKET_TOLERANCE, 2 percentage points)."""
    tolerance = tolerance if tolerance is not None else config.BACKTEST_LLTV_BUCKET_TOLERANCE
    lltv_bucket = round(lltv_frac / tolerance) * tolerance
    return (symbol, oracle_class, round(lltv_bucket, 6))


def has_dex_exit(collateral_token, price, block, size_usd=None):
    """Task 3's own machinery (dex.best_route + the no-key aggregator), checked ONCE at a
    representative size and at a LIVE block (config.BACKTEST_DEX_CHECK_SIZE_USD docstring
    explains why this is a mild, explicitly flagged look-ahead rather than a true historical
    check). True if either source finds any depth at all."""
    size_usd = size_usd if size_usd is not None else config.BACKTEST_DEX_CHECK_SIZE_USD
    if collateral_token.lower() == ZERO or price is None:
        return False
    raw = int(size_usd * 10**42 / price)
    # Cheapest checks first (aggregator: one HTTP call; direct pair: up to 4 eth_calls) -- the
    # full multi-hop dex.best_route (up to dozens of eth_calls per market) only as a last
    # resort, since this runs across ~70-90 markets here, not the main universe's 15.
    try:
        (decimals,) = chain.call(collateral_token, "decimals", [], block)
        if dex.aggregator_quote(collateral_token, USDC, raw, decimals) is not None:
            return True
    except Exception:
        pass
    if dex.best_quote(collateral_token, USDC, raw, block) is not None:
        return True
    return dex.best_route(collateral_token, USDC, raw, block) is not None


def pull_bad_debt_by_day(market_id, window_start_ts, window_end_ts):
    """{day: total_bad_debt_usd} for a market's real Liquidate events in the window -- reuses
    api.get_market_liquidations, same source liq_edge.py uses. USD via /1e6 since loan token is
    always USDC (6 decimals) in this universe."""
    events = api.get_market_liquidations(market_id, before_timestamp=window_end_ts, since_timestamp=window_start_ts)
    out = {}
    for e in events:
        bad_debt = int(e["data"]["badDebtAssets"])
        if bad_debt > 0:
            out[_day(e["timestamp"])] = out.get(_day(e["timestamp"]), 0.0) + bad_debt / 1e6
    return out


AAVE_USDC_DEFILLAMA_POOL = spread.AAVE_USDC_DEFILLAMA_POOL


def aave_daily_history():
    """{day: supply_rate_fraction} for Aave v3 USDC, the BENCH strategy's only market -- same
    DefiLlama pool spread.aave_usdc_supply_rate_90d_avg already uses, pulled in full instead of
    just a 90d trailing average."""
    r = requests.get(f"https://yields.llama.fi/chart/{AAVE_USDC_DEFILLAMA_POOL}", timeout=30)
    r.raise_for_status()
    out = {}
    for p in r.json()["data"]:
        if p.get("apyBase") is None:
            continue
        ts = int(dt.datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")).timestamp())
        out[_day(ts)] = p["apyBase"] / 100
    return out


def simulate_bench(aave_history, window_start_day, window_end_day, aum_usd=None):
    """100% Aave v3 USDC, compounded daily off the real historical rate -- no frictions (gas-
    free, infinitely liquid, no rate impact at this size for Aave's own depth)."""
    aum_usd = aum_usd if aum_usd is not None else config.BACKTEST_AUM_USD
    equity = aum_usd
    curve = []
    day = window_start_day
    while day <= window_end_day:
        rate = aave_history.get(day)
        if rate is not None:
            equity *= (1 + rate) ** (1 / 365)
        curve.append((day, equity))
        day += DAY
    return {"equity_reported": curve, "equity_marked": curve, "trapped_usd_series": [(d, 0.0) for d, _ in curve], "n_switches": 0}


def precompute_static_flags(universe, block):
    """Oracle class and DEX-exit availability, each computed once (not daily) -- see their own
    docstrings for why neither needs re-evaluating per day."""
    oracle_class, has_dex = {}, {}
    for m in universe:
        oracle_class[m["market_id"]] = oracle.classify(m["oracle_address"], block)["class"]
        try:
            price = oracle.read_price(m["oracle_address"], block, max_retries=5)
        except Exception:
            price = None
        has_dex[m["market_id"]] = has_dex_exit(m["collateral_token"], price, block) if price is not None else False
    return {"oracle_class": oracle_class, "has_dex": has_dex}


def _rank_candidates(universe, histories, day, filters=None):
    scored = []
    for m in universe:
        mid = m["market_id"]
        if filters is not None:
            if filters["oracle_class"].get(mid) in ("FIXED", "UNKNOWN"):
                continue
            if not filters["has_dex"].get(mid, False):
                continue
            if is_zombie_asof(histories[mid], day):
                continue
        if day not in histories.get(mid, {}):
            continue
        apr = trailing_avg_apr(histories[mid], day, 7)
        if apr is not None:
            scored.append((apr, mid))
    scored.sort(reverse=True)
    return scored


def strategy_naive(day, universe, histories, positions, state):
    top = _rank_candidates(universe, histories, day, filters=None)[: config.BACKTEST_TOP_K]
    if not top:
        return {}, state
    w = 1.0 / len(top)
    return {mid: w for _apr, mid in top}, state


def strategy_risk_aware(day, universe, histories, positions, state):
    top = _rank_candidates(universe, histories, day, filters=state)[: config.BACKTEST_TOP_K]
    if not top:
        return {}, state
    w = 1.0 / len(top)
    return {mid: w for _apr, mid in top}, state


def strategy_rel_value(day, universe, histories, positions, state):
    """Within each same-risk bucket (bucket_key), hold the highest trailing-rate market passing
    the RISK_AWARE filters; switch only when the gap to the CURRENT holding exceeds
    config.BACKTEST_SWITCH_THRESHOLD_BPS net of gas (converted to an upfront bps-equivalent cost
    on an assumed-equal per-bucket position size -- the rate-impact side of "after rate impact
    and gas" is NOT applied to the switch decision itself, only to the actual income accrual
    via income_with_dilution elsewhere; a labeled simplification, not hidden). Equal weight
    across buckets.
    """
    buckets = state.get("buckets")
    if buckets is None:
        buckets = {}
        for m in universe:
            key = bucket_key(m["symbol"], state["oracle_class"].get(m["market_id"]), m["lltv"] / mm.WAD)
            buckets.setdefault(key, []).append(m["market_id"])
        state["buckets"] = buckets
    current_holding = state.setdefault("current_holding_per_bucket", {})
    gap_log = state.setdefault("gap_log", [])

    target = {}
    for key, market_ids in buckets.items():
        candidates = []
        for mid in market_ids:
            if state["oracle_class"].get(mid) in ("FIXED", "UNKNOWN"):
                continue
            if not state["has_dex"].get(mid, False):
                continue
            if day not in histories.get(mid, {}):
                continue
            if is_zombie_asof(histories[mid], day):
                continue
            apr = trailing_avg_apr(histories[mid], day, 7)
            if apr is not None:
                candidates.append((apr, mid))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        best_apr, best_mid = candidates[0]
        held_mid = current_holding.get(key)
        if held_mid is None:
            current_holding[key] = best_mid
        elif held_mid != best_mid:
            held_apr = next((a for a, m in candidates if m == held_mid), None)
            if held_apr is None:
                current_holding[key] = best_mid  # held market dropped out of the bucket's own candidate set
            else:
                gap_bps = (best_apr - held_apr) * 10_000
                approx_position_usd = config.BACKTEST_AUM_USD / max(1, len(buckets))
                gas_bps_equivalent = (config.BACKTEST_GAS_COST_USD / approx_position_usd) * 10_000
                net_gap_bps = gap_bps - gas_bps_equivalent
                gap_log.append({"day": day, "bucket": key, "gap_bps": gap_bps, "net_gap_bps": net_gap_bps})
                if net_gap_bps > config.BACKTEST_SWITCH_THRESHOLD_BPS:
                    current_holding[key] = best_mid
        target[current_holding[key]] = target.get(current_holding[key], 0) + 1

    n = sum(target.values())
    if n == 0:
        return {}, state
    return {mid: cnt / n for mid, cnt in target.items()}, state


def simulate(universe, histories, bad_debt_by_market, strategy_fn, window_start_day, window_end_day, filters=None, aum_usd=None, rebalance_days=None, gas_usd=None, fee_frac=0.0):
    """The shared mechanics for every Morpho strategy (NAIVE/RISK_AWARE/REL_VALUE): daily income
    accrual with own-deposit dilution, pro-rata bad-debt hits, liquidity-capped withdrawals
    (the shortfall tracked as "trapped", not silently dropped or silently forced through), and
    periodic rebalancing via a pluggable strategy_fn(day, universe, histories, positions, state)
    -> (target_weights, state). strategy_fn is never shown data past `day` (no look-ahead).
    """
    aum_usd = aum_usd if aum_usd is not None else config.BACKTEST_AUM_USD
    rebalance_days = rebalance_days if rebalance_days is not None else config.BACKTEST_REBALANCE_DAYS
    gas_usd = gas_usd if gas_usd is not None else config.BACKTEST_GAS_COST_USD

    positions = {}
    cash = aum_usd
    equity_reported, equity_marked, trapped_series = [], [], []
    n_switches = 0
    trade_log = []
    current_trapped = 0.0
    state = dict(filters) if filters else {}
    target_now = {}

    day = window_start_day
    last_rebalance = None
    while day <= window_end_day:
        for mid in list(positions.keys()):
            rec = histories.get(mid, {}).get(day)
            if rec is None:
                continue
            x = positions[mid]
            income = income_with_dilution(x, rec.get("supplyAssetsUsd", 0.0), rec.get("borrowAssetsUsd"), rec.get("rateAtTarget"), fee_frac)
            positions[mid] = x + income
            bad_debt_today = bad_debt_by_market.get(mid, {}).get(day, 0.0)
            supply_usd = rec.get("supplyAssetsUsd")
            if bad_debt_today > 0 and supply_usd:
                my_share = positions[mid] / supply_usd
                hit = min(positions[mid], my_share * bad_debt_today)
                positions[mid] -= hit

        if last_rebalance is None or day - last_rebalance >= rebalance_days * DAY:
            target_weights, state = strategy_fn(day, universe, histories, positions, state)
            total_equity = cash + sum(positions.values())
            target_now = {mid: w * total_equity for mid, w in target_weights.items()}

            for mid in list(positions.keys()):
                want = target_now.get(mid, 0.0)
                have = positions[mid]
                if want < have - 1e-6:
                    excess = have - want
                    rec = histories.get(mid, {}).get(day)
                    liquidity = max(0.0, (rec.get("supplyAssetsUsd", 0.0) - rec.get("borrowAssetsUsd", 0.0))) if rec else 0.0
                    withdrawable = min(excess, max(0.0, liquidity))
                    positions[mid] -= withdrawable
                    cash += withdrawable
                    if withdrawable > 1e-6:
                        cash -= gas_usd
                        n_switches += 1
                        trade_log.append({"day": day, "market_id": mid, "action": "exit", "usd": withdrawable})
                    if positions[mid] <= 0:
                        del positions[mid]

            for mid, want in target_now.items():
                have = positions.get(mid, 0.0)
                if want > have + 1e-6:
                    deploy = min(want - have, max(0.0, cash - gas_usd))
                    if deploy > 1e-6:
                        positions[mid] = have + deploy
                        cash -= deploy
                        cash -= gas_usd
                        n_switches += 1
                        trade_log.append({"day": day, "market_id": mid, "action": "enter", "usd": deploy})

            current_trapped = sum(max(0.0, positions[mid] - target_now.get(mid, 0.0)) for mid in positions)
            last_rebalance = day

        reported_equity = cash + sum(positions.values())
        marked_equity = cash
        for mid, x in positions.items():
            rec = histories.get(mid, {}).get(day)
            mark_frac = 1.0
            if rec is not None and is_zombie_asof(histories[mid], day):
                collateral_usd, borrow_usd = rec.get("collateralAssetsUsd"), rec.get("borrowAssetsUsd")
                if collateral_usd is not None and borrow_usd:
                    mark_frac = min(1.0, collateral_usd / borrow_usd)
            marked_equity += x * mark_frac
        equity_reported.append((day, reported_equity))
        equity_marked.append((day, marked_equity))
        trapped_series.append((day, current_trapped))

        day += DAY

    return {
        "equity_reported": equity_reported,
        "equity_marked": equity_marked,
        "trapped_usd_series": trapped_series,
        "n_switches": n_switches,
        "trade_log": trade_log,
        "final_cash": cash,
        "final_positions": positions,
        "gap_log": state.get("gap_log", []),
    }


def performance_stats(equity_curve, bench_curve=None):
    """Annualized return, vol, Sharpe vs bench, max drawdown -- from a daily equity series
    [(day, equity), ...]. Sharpe uses daily log-ish simple returns, no risk-free subtraction
    beyond bench_curve itself (bench IS the risk-free proxy here, Aave USDC)."""
    values = [v for _d, v in equity_curve]
    if len(values) < 2 or values[0] <= 0:
        return {"ann_return": None, "ann_vol": None, "sharpe_vs_bench": None, "max_drawdown": None}
    rets = [(values[i] / values[i - 1] - 1) for i in range(1, len(values)) if values[i - 1] > 0]
    n_years = len(values) / 365.25
    total_return = values[-1] / values[0] - 1
    ann_return = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else None
    mean_ret = sum(rets) / len(rets) if rets else 0.0
    variance = sum((r - mean_ret) ** 2 for r in rets) / len(rets) if rets else 0.0
    ann_vol = (variance**0.5) * (365.25**0.5)

    sharpe = None
    if bench_curve is not None and len(bench_curve) == len(equity_curve):
        bench_values = [v for _d, v in bench_curve]
        excess = [
            (values[i] / values[i - 1] - 1) - (bench_values[i] / bench_values[i - 1] - 1)
            for i in range(1, len(values))
            if values[i - 1] > 0 and bench_values[i - 1] > 0
        ]
        if excess:
            mean_excess = sum(excess) / len(excess)
            var_excess = sum((e - mean_excess) ** 2 for e in excess) / len(excess)
            std_excess = var_excess**0.5
            sharpe = (mean_excess * 365.25) / (std_excess * (365.25**0.5)) if std_excess > 0 else None

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)

    return {"ann_return": ann_return, "ann_vol": ann_vol, "sharpe_vs_bench": sharpe, "max_drawdown": max_dd}
