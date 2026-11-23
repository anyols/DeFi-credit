
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd

import config
from kvr import backtest, basis, chain, plots, spread


def main():
    window_end_ts = chain.w3().eth.get_block(config.SNAPSHOT_BLOCK)["timestamp"]
    window_end_day = backtest._day(window_end_ts)
    window_start_day = window_end_day - (config.BASIS_LOOKBACK_DAYS - 1) * backtest.DAY

    markets_df = pd.read_parquet("data/processed/markets.parquet")
    wsteth_row = markets_df[markets_df["collateral_symbol"] == "wstETH"].iloc[0]
    wsteth_market_id = wsteth_row["market_id"]
    lltv = int(wsteth_row["lltv"]) / 10**18
    print(f"wstETH/USDC market: {wsteth_market_id}, LLTV={lltv:.2%}")

    print("Pulling wstETH/USDC Morpho history...")
    wsteth_morpho_history = backtest.pull_daily_history(wsteth_market_id, window_start_day, window_end_day)

    print("Pulling ETH perp funding history (Binance)...")
    funding_history = basis.fetch_funding_history(window_start_day, window_end_day)
    print(f"{len(funding_history)} funding events")

    print("Sampling wstETH staking yield on-chain...")
    staking_samples = basis.fetch_wsteth_staking_yield_samples(window_start_day, window_end_day)
    print(f"{len(staking_samples)} samples")

    print("Pulling Aave USDC history (LEND reference)...")
    aave_history = backtest.aave_daily_history()
    lend_rate_90d = spread.aave_usdc_supply_rate_90d_avg()

    print("Computing daily carry...")
    carries = []
    day = window_start_day
    while day <= window_end_day:
        carries.append(basis.daily_carry(day, funding_history, staking_samples, wsteth_morpho_history))
        day += backtest.DAY

    basis_series = [(c["day"], c["basis_carry"]) for c in carries]
    levered_series = [(c["day"], c["levered_carry"]) for c in carries]

    def stats_for(series, label):
        vals = [v for _d, v in series if v is not None]
        n = len(vals)
        if n == 0:
            return {"label": label, "n_days": 0}
        mean = sum(vals) / n
        variance = sum((v - mean) ** 2 for v in vals) / n
        vol = variance**0.5
        n_negative = sum(1 for v in vals if v < 0)
        rolling_30d = [sum(vals[i : i + 30]) / min(30, len(vals[i : i + 30])) for i in range(0, max(1, n - 29))]
        worst_30d = min(rolling_30d) if rolling_30d else None
        sharpe = (mean - (lend_rate_90d or 0)) / vol if vol > 0 else None
        return {
            "label": label,
            "n_days": n,
            "mean_annualized": mean,
            "vol_annualized": vol,
            "pct_days_negative": n_negative / n,
            "worst_30d_avg_annualized": worst_30d,
            "sharpe_vs_lend": sharpe,
        }

    table = {
        "LEND (Aave 90d avg)": {"label": "LEND", "mean_annualized": lend_rate_90d},
        "BASIS (1x)": stats_for(basis_series, "BASIS"),
        "LEVERED BASIS": stats_for(levered_series, "LEVERED BASIS"),
    }
    print(json.dumps(table, indent=2, default=str))

    n_funding_days = sum(1 for d in range(window_start_day, window_end_day + 1, backtest.DAY) if basis.daily_funding_rate(funding_history, d) is not None)
    n_funding_negative = sum(
        1 for d in range(window_start_day, window_end_day + 1, backtest.DAY) if (basis.daily_funding_rate(funding_history, d) or 0) < 0
    )
    depeg_threshold = basis.depeg_to_liquidation(config.BASIS_TARGET_LTV, lltv)
    leverage = 1 / (1 - config.BASIS_TARGET_LTV)
    margin_buffer = basis.perp_margin_buffer_needed(leverage, config.BASIS_MARGIN_STRESS_PCT)

    risks = {
        "pct_days_funding_negative": n_funding_negative / n_funding_days if n_funding_days else None,
        "target_ltv": config.BASIS_TARGET_LTV,
        "lltv": lltv,
        "leverage": leverage,
        "depeg_pct_to_liquidation": depeg_threshold,
        "margin_stress_pct": config.BASIS_MARGIN_STRESS_PCT,
        "margin_buffer_needed_frac_of_equity": margin_buffer,
    }
    print("\nRisks:", json.dumps(risks, indent=2))

    figure = plots.plot_basis_carry(lend_rate_90d, basis_series, levered_series)

    out = {
        "window_start_day": window_start_day,
        "window_end_day": window_end_day,
        "table": table,
        "risks": risks,
        "figure": figure,
    }
    Path("data/processed/basis.json").write_text(json.dumps(out, indent=2, default=str))
    print("\nSaved data/processed/basis.json")


if __name__ == "__main__":
    main()
