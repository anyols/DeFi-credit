import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import config
from kvr import backtest, chain, plots, stream_case


def spot_check_onchain(universe, histories, window_start_day, window_end_day, n_dates=5, n_markets=5):
    """Review requirement: verify a SAMPLE of the API's historical daily state against a real
    on-chain read, at n_dates evenly spaced dates and n_markets markets per date."""
    checks = []
    for i in range(n_dates):
        frac = i / (n_dates - 1) if n_dates > 1 else 0
        target_day = window_start_day + int(frac * (window_end_day - window_start_day))
        target_day = backtest._day(target_day)
        block = stream_case.find_block_by_timestamp(target_day, w3=chain.w3())
        for m in universe[:n_markets]:
            mid = m["market_id"]
            rec = histories.get(mid, {}).get(target_day)
            if rec is None:
                continue
            try:
                raw = chain.call_morpho("market", [bytes.fromhex(mid[2:])], block)
            except Exception:
                continue
            total_supply_assets, _tss, total_borrow_assets, _tbs, _lu, _fee = raw
            onchain_util = (total_borrow_assets / total_supply_assets) if total_supply_assets > 0 else 0.0
            checks.append(
                {
                    "market": m["symbol"],
                    "date": target_day,
                    "block": block,
                    "api_utilization": rec.get("utilization"),
                    "onchain_utilization": onchain_util,
                    "diff": abs((rec.get("utilization") or 0) - onchain_util),
                }
            )
    return checks


def exclusion_counts(universe, filters, histories, window_start_day, window_end_day):
    oracle_excluded = sum(1 for m in universe if filters["oracle_class"].get(m["market_id"]) in ("FIXED", "UNKNOWN"))
    dex_excluded = sum(1 for m in universe if not filters["has_dex"].get(m["market_id"], False))
    zombie_ever = 0
    for m in universe:
        hist = histories.get(m["market_id"], {})
        if any(backtest.is_zombie_asof(hist, d) for d in range(window_start_day, window_end_day + 1, backtest.DAY * 7)):
            zombie_ever += 1
    return {"oracle_class_excluded": oracle_excluded, "no_dex_exit_excluded": dex_excluded, "ever_zombie_excluded": zombie_ever}


def days_trapped(trapped_series):
    return sum(1 for _d, v in trapped_series if v > 1.0)


def run_one(name, universe, histories, bad_debt_by_market, strategy_fn, window_start_day, window_end_day, filters=None, **kwargs):
    result = backtest.simulate(universe, histories, bad_debt_by_market, strategy_fn, window_start_day, window_end_day, filters=filters, **kwargs)
    stats = backtest.performance_stats(result["equity_reported"])
    return result, stats


def main():
    window_end_ts = chain.w3().eth.get_block(config.SNAPSHOT_BLOCK)["timestamp"]
    window_end_day = backtest._day(window_end_ts)
    window_start_day = window_end_day - (config.BACKTEST_LOOKBACK_DAYS - 1) * backtest.DAY

    print(f"Window: {window_start_day} to {window_end_day} ({config.BACKTEST_LOOKBACK_DAYS} days)")
    print("Discovering universe...")
    universe = backtest.discover_universe()
    print(f"{len(universe)} markets in universe (current supply >= ${config.BACKTEST_MIN_SUPPLY_USD:,.0f})")

    print("Pulling daily histories...")
    histories = {}
    for i, m in enumerate(universe):
        histories[m["market_id"]] = backtest.pull_daily_history(m["market_id"], window_start_day, window_end_day)
        print(f"  {i+1}/{len(universe)} {m['symbol']}: {len(histories[m['market_id']])} days")

    print("Pulling bad debt events...")
    bad_debt_by_market = {m["market_id"]: backtest.pull_bad_debt_by_day(m["market_id"], window_start_day, window_end_day) for m in universe}
    total_bad_debt_events = sum(len(v) for v in bad_debt_by_market.values())
    print(f"{total_bad_debt_events} market-days with realized bad debt across the universe")

    print("Pulling Aave USDC history...")
    aave_history = backtest.aave_daily_history()

    print("Precomputing static filters (oracle class, DEX exit)...")
    filters = backtest.precompute_static_flags(universe, config.SNAPSHOT_BLOCK)
    print(f"  oracle FIXED/UNKNOWN: {sum(1 for v in filters['oracle_class'].values() if v in ('FIXED','UNKNOWN'))}/{len(universe)}")
    print(f"  no DEX exit: {sum(1 for v in filters['has_dex'].values() if not v)}/{len(universe)}")

    print("Spot-checking 5 dates on-chain...")
    spot_checks = spot_check_onchain(universe, histories, window_start_day, window_end_day)
    for c in spot_checks:
        print(f"  {c['market']} @ day {c['date']} block {c['block']}: api_util={c['api_utilization']:.4f} onchain_util={c['onchain_utilization']:.4f} diff={c['diff']:.4f}")

    print("Running BENCH...")
    bench = backtest.simulate_bench(aave_history, window_start_day, window_end_day)
    bench_stats = backtest.performance_stats(bench["equity_reported"])

    print("Running NAIVE...")
    naive, naive_stats = run_one("NAIVE", universe, histories, bad_debt_by_market, backtest.strategy_naive, window_start_day, window_end_day)

    print("Running RISK_AWARE...")
    risk_aware, risk_aware_stats = run_one("RISK_AWARE", universe, histories, bad_debt_by_market, backtest.strategy_risk_aware, window_start_day, window_end_day, filters=filters)

    print("Running REL_VALUE...")
    rel_value, rel_value_stats = run_one("REL_VALUE", universe, histories, bad_debt_by_market, backtest.strategy_rel_value, window_start_day, window_end_day, filters=filters)

    strategies = {"BENCH": (bench, bench_stats), "NAIVE": (naive, naive_stats), "RISK_AWARE": (risk_aware, risk_aware_stats), "REL_VALUE": (rel_value, rel_value_stats)}

    table = {}
    for name, (result, stats) in strategies.items():
        bench_stats_vs = backtest.performance_stats(result["equity_reported"], bench["equity_reported"]) if name != "BENCH" else stats
        table[name] = {
            "ann_return": stats["ann_return"],
            "ann_vol": stats["ann_vol"],
            "sharpe_vs_bench": bench_stats_vs["sharpe_vs_bench"],
            "max_drawdown": stats["max_drawdown"],
            "days_trapped": days_trapped(result["trapped_usd_series"]),
            "usd_trapped_at_end": result["trapped_usd_series"][-1][1] if result["trapped_usd_series"] else 0.0,
            "n_switches": result["n_switches"],
            "final_equity_reported": result["equity_reported"][-1][1],
            "final_equity_marked": result["equity_marked"][-1][1],
        }
    table["RISK_AWARE"]["exclusions"] = exclusion_counts(universe, filters, histories, window_start_day, window_end_day)
    table["REL_VALUE"]["exclusions"] = table["RISK_AWARE"]["exclusions"]

    print(json.dumps(table, indent=2, default=str))

    # REL_VALUE bucket gap analysis (frequency/persistence of gaps > threshold, P&L captured)
    gap_log = rel_value.get("gap_log", [])
    bucket_gap_summary = {}
    for g in gap_log:
        key = str(g["bucket"])
        s = bucket_gap_summary.setdefault(key, {"n_gaps_logged": 0, "n_switched": 0})
        s["n_gaps_logged"] += 1
        if g["net_gap_bps"] > config.BACKTEST_SWITCH_THRESHOLD_BPS:
            s["n_switched"] += 1
    print("\nREL_VALUE bucket gap summary:", json.dumps(bucket_gap_summary, indent=2))
    # vs RISK_AWARE, not NAIVE: both use the SAME point-in-time filters, differing only in
    # REL_VALUE's bucket-relative-value switching -- comparing against NAIVE's own reported
    # equity would compare a real number to an illusory, zombie-rate-compounded one.
    pnl_captured_reported_usd = rel_value["equity_reported"][-1][1] - risk_aware["equity_reported"][-1][1]
    pnl_captured_marked_usd = rel_value["equity_marked"][-1][1] - risk_aware["equity_marked"][-1][1]
    print(f"REL_VALUE vs RISK_AWARE P&L captured (reported): ${pnl_captured_reported_usd:,.2f}")
    print(f"REL_VALUE vs RISK_AWARE P&L captured (marked): ${pnl_captured_marked_usd:,.2f}")

    # Sensitivity
    sensitivity = {"K": {}, "rebalance_days": {}, "switch_threshold_bps": {}}
    for k in config.BACKTEST_SENSITIVITY_K:
        old_k = config.BACKTEST_TOP_K
        config.BACKTEST_TOP_K = k
        r, s = run_one(f"NAIVE_K{k}", universe, histories, bad_debt_by_market, backtest.strategy_naive, window_start_day, window_end_day)
        sensitivity["K"][k] = {"ann_return": s["ann_return"], "final_equity": r["equity_reported"][-1][1]}
        config.BACKTEST_TOP_K = old_k
    for rb in config.BACKTEST_SENSITIVITY_REBALANCE_DAYS:
        r, s = run_one(f"NAIVE_rb{rb}", universe, histories, bad_debt_by_market, backtest.strategy_naive, window_start_day, window_end_day, rebalance_days=rb)
        sensitivity["rebalance_days"][rb] = {"ann_return": s["ann_return"], "final_equity": r["equity_reported"][-1][1], "n_switches": r["n_switches"]}
    for sb in config.BACKTEST_SENSITIVITY_SWITCH_BPS:
        old_sb = config.BACKTEST_SWITCH_THRESHOLD_BPS
        config.BACKTEST_SWITCH_THRESHOLD_BPS = sb
        r, s = run_one(f"RELVAL_sb{sb}", universe, histories, bad_debt_by_market, backtest.strategy_rel_value, window_start_day, window_end_day, filters=dict(filters))
        sensitivity["switch_threshold_bps"][sb] = {"ann_return": s["ann_return"], "final_equity": r["equity_reported"][-1][1], "n_switches": r["n_switches"]}
        config.BACKTEST_SWITCH_THRESHOLD_BPS = old_sb
    print("\nSensitivity:", json.dumps(sensitivity, indent=2, default=str))

    figures = [
        plots.plot_backtest_equity_curves(
            {
                "BENCH (Aave)": bench["equity_reported"],
                "NAIVE (reported)": naive["equity_reported"],
                "NAIVE (marked)": naive["equity_marked"],
                "RISK_AWARE": risk_aware["equity_reported"],
                "REL_VALUE": rel_value["equity_reported"],
            }
        )
    ]

    out = {
        "evidence": "exploratory_current_universe_and_snapshot_exit_filters",
        "window_start_day": window_start_day,
        "window_end_day": window_end_day,
        "n_markets": len(universe),
        "spot_checks": spot_checks,
        "table": table,
        "bucket_gap_summary": bucket_gap_summary,
        "rel_value_pnl_captured_vs_risk_aware_reported_usd": pnl_captured_reported_usd,
        "rel_value_pnl_captured_vs_risk_aware_marked_usd": pnl_captured_marked_usd,
        "sensitivity": sensitivity,
        "figures": figures,
    }
    Path("data/processed/backtest.json").write_text(json.dumps(out, indent=2, default=str))
    print("\nSaved data/processed/backtest.json")


if __name__ == "__main__":
    main()
