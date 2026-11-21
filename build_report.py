"""Runs M5-M12 against the real, already-cached M1/M2 output and produces every figure +
number the memo needs. Separate from run_all.py (which covers the reproducible M1/M2 pull)
because this stage mixes in external data (DEX quotes, DefiLlama history, Aave, a second
chain for the Stream case) that's slower and noisier to re-pull on every run.
"""

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd

import config
from kvr import chain, dex, el, oracle, optimize, plots, positions, scenarios, spread, stream_case, stress
from kvr import morpho_math as mm

MARKET_LABELS = {
    "0x54efdee08e272e929034a8f26f7ca34b1ebe364b275391169b28c6d7db24dbc8": "idle USDC",
    "0x64d65c9a2d91c36d56fbc42d69e979335320169b3df63bf92789e2c8883fcc64": "USDC/cbBTC",
    "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49": "USDC/WBTC",
    "0xae60b71b407e0517ead445b7113a7ffa07ea4a9379d526ade541a3e9ec777cb4": "USDC/sNUSD",
}
ZERO = "0x" + "00" * 20
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
BTC_COIN = "ethereum:0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"


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
    return {
        "market_id": market_id,
        "label": MARKET_LABELS.get(market_id, market_id[:10]),
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


def slippage_curve(m, block):
    loan, collateral_token = m["row"]["loan_token"], m["row"]["collateral_token"]
    if collateral_token.lower() == ZERO:
        return None
    decimals = 8 if m["label"] in ("USDC/cbBTC", "USDC/WBTC") else 18
    sizes = [0.5, 2.5, 10, 50, 200] if decimals == 8 else [10_000, 50_000, 200_000, 1_000_000]
    sizes_raw = [int(s * 10**decimals) for s in sizes]
    points = dex.quote_curve(collateral_token, loan, sizes_raw, block)
    return dex.fit_slippage(points)


def main():
    block = config.SNAPSHOT_BLOCK
    block_ts = chain.w3().eth.get_block(block)["timestamp"]
    markets_df = pd.read_parquet("data/processed/markets.parquet")
    markets = {mid: load_market(mid, markets_df, block, block_ts) for mid in MARKET_LABELS}
    results = {}
    figures = []

    results["oracle_class"] = {
        m["label"]: ("N/A (idle market)" if m["row"]["oracle"].lower() == ZERO else oracle.classify(m["row"]["oracle"], block)["class"])
        for m in markets.values()
    }

    results["concentration"] = {m["label"]: c for m in markets.values() if (c := concentration(m)) is not None}

    distances = {m["label"]: distances_to_liquidation(m) for m in markets.values()}
    figures.append(plots.plot_distance_to_liquidation_cdf({k: v for k, v in distances.items() if v}))

    slip_fits = {mid: slippage_curve(m, block) for mid, m in markets.items()}
    results["slippage_fits"] = {markets[mid]["label"]: fit for mid, fit in slip_fits.items()}

    results["liquidity_stress"] = {
        m["label"]: stress.liquidity_stress(m["supply_assets"], m["borrow_assets"]) for m in markets.values()
    }

    stress_results, breakpoints = {}, {}
    for mid, m in markets.items():
        fit = slip_fits.get(mid)
        if fit is None or not m["positions"]:
            continue
        a, b, _cap = fit
        oc = results["oracle_class"][m["label"]]
        args = (m["positions"], m["lltv"], m["price"], m["price"], m["borrow_assets"], m["borrow_shares"])
        gap = [stress.run_scenario(*args, s, 1 + s, 1, 1, (a, b)) for s in scenarios.UNDERLYING_SHOCKS]
        gradual = [stress.run_scenario(*args, s, 1 + s, 1, 6, (a, b)) for s in scenarios.UNDERLYING_SHOCKS]
        stress_results[m["label"]] = {"gap (K=1)": gap, "gradual (K=6)": gradual}
        breakpoints[m["label"]] = stress.find_breakpoint_shock(*args, scenarios.UNDERLYING_SHOCKS, oc, False, 1, (a, b))
    if stress_results:
        figures.append(plots.plot_loss_vs_shock(stress_results, breakpoints))
    results["breakpoint_shock"] = breakpoints

    btc_prices = scenarios.fetch_daily_prices(BTC_COIN)
    lambda_btc = scenarios.exceedance_frequency(btc_prices, horizon_days=1)
    label_to_supply_assets = {m["label"]: m["supply_assets"] for m in markets.values()}
    el_results = {}
    for label, paths in stress_results.items():
        # sNUSD has no usable tail-history source (scenarios.py's own docstring explains why
        # DefiLlama's series for it would be circular) -- lambda_fn=None makes this None too,
        # per SPEC.md's "not estimable, judgment only" rule rather than inventing a number.
        lambda_fn = lambda_btc if label in ("USDC/cbBTC", "USDC/WBTC") else None
        el_results[label] = el.expected_loss_bps(paths["gap (K=1)"], label_to_supply_assets[label], lambda_fn)
    results["el_bps"] = el_results

    rf_90d = spread.aave_usdc_supply_rate_90d_avg()
    waterfalls = {}
    for mid, m in markets.items():
        if m["label"] == "idle USDC":
            continue
        try:
            supply_apy = spread.market_quarterly_supply_apy(mid)
            util_cap = spread.utilization_time_at_cap(mid)
            if supply_apy is None:
                continue
            el_bps = el_results.get(m["label"])
            el_range = (el_bps, el_bps) if isinstance(el_bps, float) else (0, 0)
            waterfalls[m["label"]] = spread.spread_waterfall(supply_apy, rf_90d, el_range, util_cap)
            waterfalls[m["label"]]["supply_apy"] = supply_apy
            waterfalls[m["label"]]["utilization_time_at_cap"] = util_cap
        except Exception:
            traceback.print_exc()
    results["spread_waterfall"] = waterfalls
    results["aave_rf_90d"] = rf_90d
    if waterfalls:
        figures.append(plots.plot_spread_waterfall(waterfalls))

    opt_markets, labels_order = [], []
    for mid, m in markets.items():
        if m["label"] == "idle USDC" or m["rate_at_target"] is None:
            continue
        opt_markets.append(
            {
                "supply_assets": m["supply_assets"],
                "borrow_assets": m["borrow_assets"],
                "rate_at_target": m["rate_at_target"],
                "fee": 0.0,
                "cap": int(m["row"]["supply_cap_usd"] * 10**6),
                "el_rate": (el_results.get(m["label"]) or 0) / 10_000,
            }
        )
        labels_order.append(m["label"])
    curve = optimize.capacity_curve(opt_markets, [b * 10**6 * 10**6 for b in (1, 5, 10, 25, 50)])
    results["capacity_curve"] = [{"budget_usd_m": p["budget"] / 10**6 / 10**6, "blended_net_yield": p["blended_net_yield"]} for p in curve]
    current_alloc = [markets[mid]["row"]["vault_supply_assets"] for mid in markets if markets[mid]["label"] in labels_order]
    optimal_alloc_1m = optimize.greedy_water_fill(opt_markets, 1_000_000 * 10**6)
    figures.append(plots.plot_capacity_curve(curve, current_alloc, optimal_alloc_1m, labels_order))

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
