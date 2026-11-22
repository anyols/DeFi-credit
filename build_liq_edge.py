"""Task 4: "When do DeFi liquidators walk away, and who gets paid when they do?" Runs
liq_edge.py over every real Liquidate event in the universe's last config.LIQ_EDGE_LOOKBACK_DAYS,
reusing each market's own DEX slippage fit from build_report.py's committed output (so this
doesn't re-quote 15 markets' worth of DEX depth from scratch -- run build_report.py first).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd

import config
from kvr import api, chain, liq_edge, plots, scenarios


def main():
    block = config.SNAPSHOT_BLOCK
    block_ts = chain.w3().eth.get_block(block)["timestamp"]
    since_ts = block_ts - config.LIQ_EDGE_LOOKBACK_DAYS * 86400
    markets_df = pd.read_parquet("data/processed/markets.parquet")

    report = json.loads(Path("data/processed/report_results.json").read_text())
    slip_fits = report["slippage_fits"]  # {label: {"source":..., "fit": [a,b,cap] or None, ...}}

    weth_prices = scenarios.fetch_price_history(config.WETH_COIN)

    summaries = {}
    delay_samples_by_market = {}
    event_counts = {}
    for _, row in markets_df.iterrows():
        label = f"USDC/{row['collateral_symbol']}"
        events = api.get_market_liquidations(row["market_id"], before_timestamp=block_ts, since_timestamp=since_ts)
        event_counts[label] = len(events)
        if not events:
            print(f"{label}: 0 liquidations in the last {config.LIQ_EDGE_LOOKBACK_DAYS} days")
            continue

        fit_entry = slip_fits.get(label) or {}
        slip_fit = tuple(fit_entry["fit"]) if fit_entry.get("fit") else None

        economics = [liq_edge.event_economics(e, row["oracle"], weth_prices, slip_fit) for e in events]

        sample = events[: config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET]  # already newest-first
        delays = []
        for e in sample:
            # Position state from just before the liquidation (pre-liquidation collateral/debt),
            # but the price search is anchored to the execution block itself, not block-1 -- see
            # liq_edge.event_economics's docstring for why those two blocks can disagree on health.
            debt, collateral = liq_edge.position_debt_and_collateral(row["market_id"], e["user"]["address"], e["blockNumber"] - 1)
            bp = liq_edge.breakeven_price(debt, collateral, int(row["lltv"]))
            delays.append(liq_edge.delay_to_liquidation_hours(row["oracle"], bp, e["blockNumber"]) if bp is not None else None)
        delay_samples_by_market[label] = delays

        summary = liq_edge.summarize_market(events, economics, delays)
        summaries[label] = summary
        print(
            f"{label}: n={summary['n_events']} insolvent={summary['n_insolvent']} "
            f"profitable(dex)={summary['profitable_share_dex']} profitable(mm)={summary['profitable_share_mm']} "
            f"left_on_table=${summary['total_left_on_table_usd']:,.2f} "
            f"median_delay={summary['median_delay_hours']}h (n={summary['n_delay_found']}/{summary['n_delay_sampled']}) "
            f"walk_away_multiplier={summary['walk_away_gas_multiplier']}"
        )

    figures = []
    if summaries:
        figures.append(plots.plot_liq_edge_profitability(summaries))
        figures.append(plots.plot_liq_edge_delay(delay_samples_by_market, config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET))
        figures.append(plots.plot_liq_edge_left_on_table(summaries))

    total_left_on_table = sum(s["total_left_on_table_usd"] for s in summaries.values())
    out = {
        "lookback_days": config.LIQ_EDGE_LOOKBACK_DAYS,
        "event_counts": event_counts,
        "total_events": sum(event_counts.values()),
        "total_left_on_table_usd": total_left_on_table,
        "summaries": summaries,
        "figures": figures,
    }
    Path("data/processed/liq_edge.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\n{out['total_events']} total events, ${total_left_on_table:,.2f} left on the table across the universe")


if __name__ == "__main__":
    main()
