import json
from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd

import config
from kvr import api, chain, liq_edge, plots, spread


def build_case_study(markets_df, block_ts, since_ts, symbol):
    """Largest recorded bad-debt event for the specified collateral."""
    row = markets_df[markets_df["collateral_symbol"] == symbol].iloc[0]
    events = api.get_market_liquidations(row["market_id"], before_timestamp=block_ts, since_timestamp=since_ts)
    insolvent = [e for e in events if int(e["data"]["badDebtAssets"]) > 0]
    if not insolvent:
        return None
    event = max(insolvent, key=lambda e: int(e["data"]["badDebtAssets"]))

    import datetime as dt

    return {
        "market": f"USDC/{symbol}",
        "market_id": row["market_id"],
        "tx_hash": event["txHash"],
        "date": dt.datetime.fromtimestamp(event["timestamp"], tz=dt.timezone.utc).isoformat(),
        "user": event["user"]["address"],
        "seized_assets_raw": event["data"]["seizedAssets"],
        "repaid_assets_usd": int(event["data"]["repaidAssets"]) / 1e6,
        "bad_debt_usd": int(event["data"]["badDebtAssets"]) / 1e6,
        "borrower_liquidations": [
            {"tx_hash": e["txHash"], "block_number": e["blockNumber"],
             "date": dt.datetime.fromtimestamp(e["timestamp"], dt.timezone.utc).isoformat(),
             "seized_assets_raw": e["data"]["seizedAssets"],
             "repaid_usdc": int(e["data"]["repaidAssets"]) / 1e6,
             "bad_debt_usdc": int(e["data"]["badDebtAssets"]) / 1e6}
            for e in reversed(events) if e["user"]["address"].lower() == event["user"]["address"].lower()
        ],
    }


def main():
    block = config.SNAPSHOT_BLOCK
    block_ts = chain.w3().eth.get_block(block)["timestamp"]
    since_ts = block_ts - config.LIQ_EDGE_LOOKBACK_DAYS * 86400
    markets_df = pd.read_parquet("data/processed/markets.parquet")

    report = json.loads(Path("data/processed/report_results.json").read_text())
    modeled_breakpoints = report["breakpoint_shock"]  # review fix 5: the stress engine's own modeled walk-away shock

    cost_of_capital_annual = spread.aave_usdc_supply_rate_90d_avg()
    if cost_of_capital_annual is None:
        cost_of_capital_annual = config.LIQ_EDGE_COST_OF_CAPITAL_ANNUAL_FALLBACK
    print(f"cost of capital (annual): {cost_of_capital_annual:.4f}" + (" (live Aave 90d avg)" if cost_of_capital_annual != config.LIQ_EDGE_COST_OF_CAPITAL_ANNUAL_FALLBACK else " (fallback)"))

    summaries = {}
    delay_samples_by_market = {}
    event_counts = {}
    volume_check_flags = []
    for _, row in markets_df.iterrows():
        symbol = row["collateral_symbol"]
        label = f"USDC/{symbol}"
        collateral_class = spread.classify_collateral(symbol)
        events = api.get_market_liquidations(row["market_id"], before_timestamp=block_ts, since_timestamp=since_ts)
        event_counts[label] = len(events)
        if not events:
            print(f"{label}: 0 liquidations in the last {config.LIQ_EDGE_LOOKBACK_DAYS} days")
            continue

        with ThreadPoolExecutor(max_workers=8) as pool:
            economics = list(pool.map(
                lambda event: liq_edge.event_economics(event, row["oracle"], symbol, collateral_class, cost_of_capital_annual),
                events,
            ))

        sample = (events[: config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET]
                  if collateral_class in ("BTC", "ETH/LST") else [])
        delays = []
        for e in sample:
            try:
                debt_exec, collateral = liq_edge.position_debt_and_collateral(
                    row["market_id"], e["user"]["address"], e["blockNumber"] - 1
                )
            except Exception:
                delays.append(None)
                continue
            bp = liq_edge.breakeven_price(debt_exec, collateral, int(row["lltv"]))
            delay = liq_edge.delay_to_liquidation_hours_extended(row["oracle"], bp, e["blockNumber"]) if bp else None
            if delay and not liq_edge.validate_delay_window(
                row["market_id"], e["user"]["address"], row["oracle"], int(row["lltv"]), e["blockNumber"], delay
            )["verified"]:
                delay = None
            delays.append(delay)
        delay_samples_by_market[label] = delays

        summary = liq_edge.summarize_market(events, economics, delays)
        summary["modeled_walk_away_shock"] = modeled_breakpoints.get(label)  # review fix 5
        summaries[label] = summary
        print(
            f"{label}: n={summary['n_events']} insolvent={summary['n_insolvent']} dust={summary['n_dust']} "
            f"profitable(count)={summary['profitable_share_count']} profitable(volume)={summary['profitable_share_volume']} "
            f"left_on_table=${summary['total_left_on_table_usd']:,.2f} "
            f"median_delay={summary['median_delay_hours']}h (n={summary['n_delay_found']}/{summary['n_delay_sampled']}) "
            f"walk_away_observed={summary['walk_away_gas_multiplier_observed']} walk_away_modeled={summary['modeled_walk_away_shock']}"
        )

        # Review fix 1: if volume-weighted profitability is < 90% for a liquid (BTC/ETH-family)
        # market, surface the full per-event decomposition instead of quietly moving on.
        if collateral_class in ("BTC", "ETH/LST") and summary["profitable_share_volume"] is not None and summary["profitable_share_volume"] < 0.9:
            volume_check_flags.append(label)
            print(f"  !! {label}: volume-weighted profitability {summary['profitable_share_volume']} < 90% -- per-event decomposition:")
            for e in economics:
                if e is not None and not e["is_dust"]:
                    print(
                        f"     bonus=${e['oracle_implied_bonus_usd']:.2f} basis=${e['oracle_market_basis_usd']:.2f} "
                        f"gas=${(e['gas_usd'] or 0):.4f} exit_cost=${(e['exit_cost_usd'] or 0):.2f} profit=${e['profit_usd']}"
                    )

    case_study = build_case_study(markets_df, block_ts, since_ts, "RLP")  # review fix 3

    figures = []
    if summaries:
        figures.append(plots.plot_liq_edge_profitability(summaries))
        figures.append(plots.plot_liq_edge_delay(delay_samples_by_market, config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET))
        figures.append(plots.plot_liq_edge_left_on_table(summaries))

    total_left_on_table = sum(s["total_left_on_table_usd"] for s in summaries.values())
    out = {
        "lookback_days": config.LIQ_EDGE_LOOKBACK_DAYS,
        "start_timestamp": since_ts,
        "end_timestamp": block_ts,
        "profitability_basis": "historical_oracle_mark_minus_receipt_gas_and_modeled_exit_cost",
        "realized_liquidator_pnl_available": False,
        "cost_of_capital_annual": cost_of_capital_annual,
        "event_counts": event_counts,
        "total_events": sum(event_counts.values()),
        "total_left_on_table_usd": total_left_on_table,
        "volume_weighted_profitability_below_90pct": volume_check_flags,
        "rlp_case_study": case_study,
        "summaries": summaries,
        "figures": figures,
    }
    Path("data/processed/liq_edge.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\n{out['total_events']} total events, ${total_left_on_table:,.2f} left on the table across the universe")
    if volume_check_flags:
        print(f"Volume-weighted profitability < 90% for: {volume_check_flags} -- decomposition printed above")
    if case_study:
        print(f"\nRLP case study: {json.dumps(case_study, indent=2, default=str)}")


if __name__ == "__main__":
    main()
