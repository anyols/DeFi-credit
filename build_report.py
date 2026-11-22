"""Runs M5-M12 against the real universe (data/processed/markets.parquet -- the top
config.TOP_N_MARKETS USDC markets by supply, per Task 1's scope change) and produces every
figure + number the memo needs. Separate from run_all.py (which covers the reproducible M1/M2
pull) because this stage mixes in external data (DEX quotes, DefiLlama history, Aave, a second
chain for the Stream case) that's slower and noisier to re-pull on every run.
"""

import datetime as dt
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd

import config
from kvr import api, chain, dex, el, oracle, optimize, plots, positions, scenarios, spread, stream_case, stress
from kvr import morpho_math as mm

ZERO = "0x" + "00" * 20
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
BTC_COIN = "ethereum:0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
_2020_03_12 = dt.datetime(2020, 3, 12, tzinfo=dt.timezone.utc).timestamp()


def load_market(market_id, markets_df, block, block_ts):
    row = markets_df[markets_df["market_id"] == market_id].iloc[0]
    raw_market = positions.fetch_market_state(market_id, block)
    supply_assets, borrow_assets, supply_shares, borrow_shares = positions.accrued_market_totals(market_id, raw_market, block_ts)
    pos_df = pd.read_parquet(f"data/processed/positions_{market_id[2:10]}.parquet")
    pos_list = [
        {"user": r.user, "borrow_shares": int(r.borrow_shares), "collateral": int(r.collateral)}
        for r in pos_df.itertuples()
        if int(r.borrow_shares) > 0
    ]
    price = oracle.read_price(row["oracle"], block) if row["oracle"].lower() != ZERO else None
    rate_at_target = None
    if row["irm"].lower() != ZERO:
        (rate_at_target,) = chain.call(row["irm"], "rateAtTarget", [bytes.fromhex(market_id[2:])], block)
    symbol = row["collateral_symbol"]
    return {
        "market_id": market_id,
        "symbol": symbol,
        "label": f"USDC/{symbol}",
        "row": row,
        "supply_assets": supply_assets,
        "borrow_assets": borrow_assets,
        "supply_shares": supply_shares,
        "borrow_shares": borrow_shares,
        "positions": pos_list,
        "price": price,
        "rate_at_target": rate_at_target,
        "lltv": int(row["lltv"]),
    }


def distances_to_liquidation(m):
    out = []
    if m["price"] is None or m["lltv"] == 0:
        return out
    for p in m["positions"]:
        borrowed = mm.borrowed(p["borrow_shares"], m["borrow_assets"], m["borrow_shares"])
        if borrowed == 0 or p["collateral"] == 0:
            continue
        price_breakeven = mm.mul_div_up(borrowed * 10**18, 10**36, p["collateral"] * m["lltv"])
        drop = max(0.0, 1 - price_breakeven / m["price"])
        out.append((drop, borrowed / 10**6))
    return out


def concentration(m):
    debts = sorted((mm.borrowed(p["borrow_shares"], m["borrow_assets"], m["borrow_shares"]) for p in m["positions"]), reverse=True)
    total = sum(debts)
    if total == 0:
        return None
    shares = [d / total for d in debts]
    return {"n_borrowers": len(debts), "top1_share": shares[0], "top5_share": sum(shares[:5]), "hhi": sum(s**2 for s in shares)}


def usd_to_collateral_raw(usd_amount, price):
    """Inverts Morpho's oracle price convention (collateral_raw * price // 1e36 == loan_raw,
    loan token always USDC/1e6 in this universe) to get a raw collateral size for a target USD
    notional -- decimals-agnostic, so it works for any collateral without needing its decimals.
    """
    return int(usd_amount * 10**42 / price)


def detect_redemption_exit(collateral_token, symbol, block):
    """Task 3: for collateral with no DEX depth from either source, checks for a VERIFIED
    issuer-redemption path instead of guessing one from the symbol/collateral class alone -- an
    unverified "probably NAV-like" label is not a finding (this project's own rule). ERC4626's
    totalAssets() succeeding means this is a real vault share with a redeem()/withdraw() path;
    Pendle PT notes (classify_collateral) redeem 1:1 for the underlying at maturity, a different
    but equally real mechanism that doesn't implement totalAssets(). Tried by hand against every
    zero-depth market in this universe: 4 of 10 (wstUSR, fxSAVE, apyUSD, USPC) are real ERC4626
    vaults; the original review's own EXCHANGE_RATE-oracle heuristic turned out to match NONE of
    them (they're all MARKET or FIXED oracle class here), so that guess is not used.
    Returns a label string, or None if no redemption path could be verified.
    """
    try:
        chain.call(collateral_token, "totalAssets", [], block)
        return "redemption exit (ERC4626 vault), not modeled by DEX depth"
    except Exception:
        pass
    if spread.classify_collateral(symbol) == "PT (fixed maturity)":
        return "redemption exit at maturity (Pendle PT), not modeled by DEX depth"
    return None


def slippage_curve(m, block):
    """Task 3: quotes on-chain depth (direct pair + multi-hop via WETH/WBTC -- dex.best_route)
    AND a no-key aggregator across dex.QUOTE_SIZES_TOKEN (USD-ish sizes, converted to this
    market's own collateral via its own oracle price), and uses whichever is deeper
    (dex.deeper_fit). Review addendum: "wstETH having zero depth is a bug, not a result" -- the
    multi-hop routing fixes that specific case; detect_redemption_exit covers collateral with
    genuinely no DEX depth at all, instead of silently reporting zero.
    Returns {"source": "onchain"|"aggregator"|"redemption_exit"|None, "fit": (...) or None, "redemption_note": str or None}.
    """
    collateral_token = m["row"]["collateral_token"]
    if collateral_token.lower() == ZERO or m["price"] is None:
        return {"source": None, "fit": None, "redemption_note": None}
    sizes_raw = [usd_to_collateral_raw(usd, m["price"]) for usd in dex.QUOTE_SIZES_TOKEN]
    onchain_points = dex.quote_curve(collateral_token, USDC, sizes_raw, block)
    try:
        (decimals,) = chain.call(collateral_token, "decimals", [], block)
        aggregator_points = dex.aggregator_quote_curve(collateral_token, USDC, sizes_raw, decimals)
    except Exception:
        aggregator_points = []
    result = dex.deeper_fit(onchain_points, aggregator_points)
    if result is None:
        note = detect_redemption_exit(collateral_token, m["symbol"], block)
        return {"source": "redemption_exit" if note else None, "fit": None, "redemption_note": note}
    source, fit = result
    return {"source": source, "fit": fit, "redemption_note": None}


def main():
    block = config.SNAPSHOT_BLOCK
    block_ts = chain.w3().eth.get_block(block)["timestamp"]
    markets_df = pd.read_parquet("data/processed/markets.parquet")
    markets = {mid: load_market(mid, markets_df, block, block_ts) for mid in markets_df["market_id"]}
    results = {}
    figures = []

    # Task 2 review point 3: zombie classifier. Excluded below from spreads (spread_waterfall)
    # and the optimizer (opt_markets) -- the cross-section's own exclusion is in
    # build_cross_section.py, which runs over a much wider market pool.
    zombies = {}
    for mid, m in markets.items():
        z = spread.classify_zombie(mid, before_timestamp=block_ts)
        if z["is_zombie"]:
            zombies[mid] = {
                "label": m["label"],
                "collateral": m["symbol"],
                "supply_usd": m["supply_assets"] / 1e6,
                "days_stuck": z["days_stuck"],
                "days_stuck_is_lower_bound": z["days_stuck_is_lower_bound"],
                "last_repay_date": dt.datetime.fromtimestamp(z["last_repay_timestamp"], tz=dt.timezone.utc).date().isoformat()
                if z["last_repay_timestamp"]
                else None,
                "days_since_repay": z["days_since_repay"],
                "apr": spread.onchain_supply_apr(mid, block),
            }
    results["zombies"] = zombies
    results["zombie_supply_usd_total"] = sum(z["supply_usd"] for z in zombies.values())
    healthy = {mid: m for mid, m in markets.items() if mid not in zombies}
    print(f"{len(zombies)} zombie markets excluded from spreads/optimizer, ${results['zombie_supply_usd_total']:,.0f} total supply")

    results["oracle_class"] = {
        m["label"]: ("N/A" if m["row"]["oracle"].lower() == ZERO else oracle.classify(m["row"]["oracle"], block)["class"])
        for m in markets.values()
    }

    results["concentration"] = {m["label"]: c for m in markets.values() if (c := concentration(m)) is not None}

    distances = {m["label"]: distances_to_liquidation(m) for m in markets.values()}
    figures.append(plots.plot_distance_to_liquidation_cdf({k: v for k, v in distances.items() if v}))

    slip_fits = {mid: slippage_curve(m, block) for mid, m in markets.items()}
    results["slippage_fits"] = {markets[mid]["label"]: v for mid, v in slip_fits.items()}
    results["redemption_exit_collateral"] = {
        markets[mid]["label"]: v["redemption_note"] for mid, v in slip_fits.items() if v["source"] == "redemption_exit"
    }

    results["liquidity_stress"] = {
        m["label"]: stress.liquidity_stress(m["supply_assets"], m["borrow_assets"]) for m in markets.values()
    }

    stress_results, breakpoints = {}, {}
    for mid, m in markets.items():
        fit = slip_fits.get(mid, {}).get("fit")
        if fit is None or not m["positions"]:
            continue
        a, b, _cap = fit
        oc = results["oracle_class"][m["label"]]
        args = (m["positions"], m["lltv"], m["price"], m["price"], m["borrow_assets"], m["borrow_shares"])
        gap = [stress.run_scenario(*args, s, scenarios.market_shock_to_oracle_ratio(s, oc), 1, 1, (a, b)) for s in scenarios.UNDERLYING_SHOCKS]
        gradual = [stress.run_scenario(*args, s, scenarios.market_shock_to_oracle_ratio(s, oc), 1, 6, (a, b)) for s in scenarios.UNDERLYING_SHOCKS]
        stress_results[m["label"]] = {"gap (K=1)": gap, "gradual (K=6)": gradual}
        breakpoints[m["label"]] = stress.find_breakpoint_shock(*args, scenarios.UNDERLYING_SHOCKS, oc, False, 1, (a, b))
    if stress_results:
        figures.append(plots.plot_loss_vs_shock(stress_results, breakpoints))
    results["breakpoint_shock"] = breakpoints

    # Task 2 + review fixes 1/2: 1d and 7d (true K=7 path, gap kept only as an upper bound)
    # horizons, lambda reported with a Poisson 90% CI, and the 7d-path model validated against
    # each market's own realized badDebtAssets over the same sample window. Only BTC/ETH-LST
    # class collateral gets a real lambda (spread.has_tail_history_proxy) -- everything else is
    # "not estimable" (handled by loss_given_shock_curve below), never a guessed number.
    btc_prices = scenarios.fetch_price_history(BTC_COIN)
    results["btc_price_history_includes_2020_03_12"] = any(abs(ts - _2020_03_12) < 2 * 86400 for ts, _ in btc_prices)
    lambda_btc_1d = scenarios.exceedance_frequency(btc_prices, horizon_days=1)
    lambda_btc_7d = scenarios.exceedance_frequency(btc_prices, horizon_days=7, declustering_days=7)
    if lambda_btc_1d is not None:
        rate, count, ci = scenarios.lambda_with_ci(lambda_btc_1d, 0.25)
        results["lambda_btc_1d_25pct_with_90pct_ci"] = {"rate_per_year": rate, "count": count, "ci_90pct": ci, "years_covered": lambda_btc_1d.years_covered}
        # Review fix 1: count==0 at -25%/1d is a real result, not a timestamp-gap bug -- verified
        # by hand that 2020-03-12 IS in the sample and its own daily-snapshot return is exactly
        # this worst value, genuinely short of -25% (DefiLlama's once-a-day ~19:25 UTC sampling
        # misses the deeper intraday low the popular "-50%" Black Thursday figure refers to).
        results["worst_btc_1d_decline_in_sample"] = float(min(lambda_btc_1d.declustered))

    el_results = {}
    model_vs_realized = {}
    for mid, m in healthy.items():
        if not spread.has_tail_history_proxy(spread.classify_collateral(m["symbol"])):
            continue
        paths = stress_results.get(m["label"])
        if paths is None or lambda_btc_1d is None or lambda_btc_7d is None:
            continue
        gap = paths["gap (K=1)"]
        oc = results["oracle_class"][m["label"]]
        a, b, _cap = slip_fits[mid]["fit"]
        args = (m["positions"], m["lltv"], m["price"], m["price"], m["borrow_assets"], m["borrow_shares"])
        path7 = [stress.run_scenario(*args, s, scenarios.market_shock_to_oracle_ratio(s, oc), 1, 7, (a, b)) for s in scenarios.UNDERLYING_SHOCKS]

        el_1d = el.expected_loss_bps(gap, m["supply_assets"], lambda_btc_1d)
        el_7d_path = el.expected_loss_bps(path7, m["supply_assets"], lambda_btc_7d)  # primary (review fix 2)
        el_7d_gap_upper_bound = el.expected_loss_bps(gap, m["supply_assets"], lambda_btc_7d)  # upper bound only
        el_results[m["label"]] = {"1d_bps": el_1d, "7d_path_bps": el_7d_path, "7d_gap_upper_bound_bps": el_7d_gap_upper_bound}

        liqs = api.get_market_liquidations(mid, before_timestamp=block_ts)
        realized_bad_debt_raw = sum(int(l["data"]["badDebtAssets"]) for l in liqs if l.get("data"))
        years = lambda_btc_7d.years_covered
        model_vs_realized[m["label"]] = {
            "model_implied_annual_loss_usd_7d_path": (el_7d_path or 0) / 10_000 * m["supply_assets"] / 1e6,
            "model_implied_annual_loss_usd_7d_gap_upper_bound": (el_7d_gap_upper_bound or 0) / 10_000 * m["supply_assets"] / 1e6,
            "realized_annual_loss_usd": realized_bad_debt_raw / 1e6 / years,
            "n_liquidation_events_in_sample": len(liqs),
            "years_covered": years,
        }
    results["el_bps"] = el_results
    results["model_vs_realized_bad_debt"] = model_vs_realized

    # Review fix 5: for collateral with no trustworthy tail-frequency source (everything that
    # didn't get a real lambda above), a pure mechanical loss-given-shock curve instead of a
    # guessed EL -- no probability attached, explicitly labeled not estimable.
    loss_given_shock = {}
    for mid, m in markets.items():
        if spread.has_tail_history_proxy(spread.classify_collateral(m["symbol"])):
            continue
        if m["price"] is None or not m["positions"]:
            continue
        curve = stress.loss_given_shock_curve(
            m["positions"], m["lltv"], m["price"], m["borrow_assets"], m["borrow_shares"], m["supply_assets"], scenarios.DEPEG_SHOCKS
        )
        loss_given_shock[m["label"]] = curve
    results["loss_given_shock_not_estimable"] = loss_given_shock

    rf_90d = spread.aave_usdc_supply_rate_90d_avg()
    waterfalls = {}
    for mid, m in healthy.items():
        try:
            if rf_90d is None:
                continue
            supply_apy = spread.onchain_supply_apy(mid, block)
            util_cap = spread.utilization_time_at_cap(mid)
            el_entry = el_results.get(m["label"])
            if el_entry and el_entry["7d_path_bps"] is not None and el_entry["7d_gap_upper_bound_bps"] is not None:
                el_range = (min(el_entry["7d_path_bps"], el_entry["7d_gap_upper_bound_bps"]), max(el_entry["7d_path_bps"], el_entry["7d_gap_upper_bound_bps"]))
            else:
                el_range = (0, 0)
            waterfalls[m["label"]] = spread.spread_waterfall(supply_apy, rf_90d, el_range, util_cap)
            waterfalls[m["label"]]["supply_apy"] = supply_apy
            waterfalls[m["label"]]["utilization_time_at_cap"] = util_cap
        except Exception:
            traceback.print_exc()
    results["spread_waterfall"] = waterfalls
    results["aave_rf_90d"] = rf_90d
    if waterfalls:
        figures.append(plots.plot_spread_waterfall(waterfalls))

    # Optimizer (M11): zombie markets excluded (Task 2 review point 3). No vault anymore, so no
    # "current allocation" to compare against and no per-market cap -- optimize.py's own
    # el_rate_default/cap defaults (0.0 / unbounded) apply.
    opt_markets, labels_order = [], []
    for mid, m in healthy.items():
        if m["rate_at_target"] is None:
            continue
        opt_markets.append(
            {
                "supply_assets": m["supply_assets"],
                "borrow_assets": m["borrow_assets"],
                "rate_at_target": m["rate_at_target"],
                "fee": 0.0,
                "el_rate": (el_results.get(m["label"], {}).get("7d_path_bps") or 0) / 10_000,
            }
        )
        labels_order.append(m["label"])
    curve = optimize.capacity_curve(opt_markets, [b * 10**6 * 10**6 for b in (1, 5, 10, 25, 50)])
    results["capacity_curve"] = [{"budget_usd_m": p["budget"] / 10**6 / 10**6, "blended_net_yield": p["blended_net_yield"]} for p in curve]
    optimal_alloc_1m = optimize.greedy_water_fill(opt_markets, 1_000_000 * 10**6)
    figures.append(plots.plot_capacity_curve(curve, optimal_alloc_1m, labels_order))

    block_nov2 = stream_case.find_block_by_timestamp(stream_case.PRE_ANNOUNCEMENT_TIMESTAMP)
    xusd_oracle = "0x1837efFC34Bb5a96EFdA00d53560799bE3a4226E"
    xusd_class = stream_case.classify_oracle_arbitrum(xusd_oracle, block_nov2)["class"]
    xusd_conc = stream_case.concentration_metrics(stream_case.XUSD_MARKET_ID)
    xusd_bad_debt = stream_case.realized_bad_debt(stream_case.XUSD_MARKET_ID)
    results["stream_case"] = {"oracle_class": xusd_class, "concentration": xusd_conc, "realized_bad_debt": xusd_bad_debt, "block_2025_11_02": block_nov2}
    comparison = {"Arbitrum USDC/xUSD": {"hhi": xusd_conc["hhi"], "oracle_class": xusd_class}}
    for label, c in results["concentration"].items():
        comparison[label] = {"hhi": c["hhi"], "oracle_class": results["oracle_class"][label]}
    figures.append(plots.plot_stream_case(comparison))

    results["figures"] = figures
    Path("data/processed/report_results.json").write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
