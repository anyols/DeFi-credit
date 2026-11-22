"""Interactive showcase for the pinned Morpho USDC credit research."""
import gzip
import json
from pathlib import Path
import sys

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import config
from kvr import basis, historical_replay, scenarios, stress

DATA = ROOT / "data" / "processed"
FIGURES = ROOT / "reports" / "figures"
NAMES = {
    "GREEDY": "Greedy",
    "REPAY_SCREEN": "Repayment screen",
    "LIQUIDITY_SCREEN": "Liquidity screen",
    "AAVE": "Aave USDC",
}
TRACKS = {
    "Smarter lender": ("historical_replay.json", None),
    "Liquidation economics": ("liq_edge.json", "fig7_liq_edge_profitability.png"),
    "Relative value": ("backtest.json", "fig11_backtest_equity_curves.png"),
    "ETH basis trade": ("basis.json", "fig12_basis_carry.png"),
}


@st.cache_data
def read_data(filename):
    path = DATA / filename
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        return json.load(source)


@st.cache_data(max_entries=16, show_spinner=False)
def rerun(aum, cadence, min_free):
    return historical_replay.run(read_data("demo_replay.json.gz"), aum=aum,
                                 rebalance_days=cadence, min_free_usdc=min_free)


def usd(value):
    return f"${value:,.0f}"


def millions(value):
    return f"${value / 1e6:,.2f}M"


def overview():
    st.header("Research overview")
    selection = st.segmented_control("Research track", list(TRACKS), default="Smarter lender")
    if selection is None:
        return
    filename, figure = TRACKS[selection]
    data = read_data(filename)
    if selection == "Smarter lender":
        st.markdown("**Which yield survives an exit?** The point-in-time replay compares greedy ranking, "
                    "a repayment screen and a free-liquidity screen with Aave. Aave led at every tested recovery rate.")
        recovery_chart(data, 50)
    elif selection == "Liquidation economics":
        st.markdown(f"**{data['total_events']} historical liquidations.** Liquid BTC and ETH collateral "
                    "generally cleared quickly. The chart estimates profitability using modeled exit costs.")
        st.markdown("The RLP case documents roughly **$1.18M** of realized bad debt on a later liquidation.")
    elif selection == "Relative value":
        difference = data["rel_value_pnl_captured_vs_risk_aware_marked_usd"]
        st.markdown(f"**{millions(difference)} higher marked equity** versus risk-aware in the exploratory run. "
                    "The strategy ranks yield within groups defined by collateral, oracle class and similar LLTV. "
                    "The legacy comparison uses snapshot exit filters, and the groups approximate comparable risk.")
        st.caption("The chart includes reported balances. The dollar comparison above uses marked equity.")
    else:
        table = data["table"]
        st.markdown(
            f"**{table['BASIS (1x)']['mean_annualized']:.2%} at 1× and "
            f"{table['LEVERED BASIS']['mean_annualized']:.2%} levered** in the historical carry study. "
            "Levered carry had a lower Sharpe versus the lending benchmark."
        )
    if figure:
        st.image(str(FIGURES / figure), width="content")


def replay_rows(result, recovery):
    rows = []
    for key, label in NAMES.items():
        row = result["table"][key]
        equity = row["cash_after_exit_usd"] + recovery / 100 * row["collateral_covered_claim_usd"]
        rows.append({
            "Strategy": label, "Reported balance": usd(row["reported_usd"]),
            "Modeled exit cash": usd(row["cash_after_exit_usd"]),
            "Residual face claim": usd(row["residual_claim_usd"]),
            "Equity after recovery": usd(equity), "P&L": usd(equity - result["starting_capital_usd"]),
        })
    return rows


def recovery_chart(result, recovery):
    points = []
    for key, label in NAMES.items():
        row = result["table"][key]
        for case in range(0, 101, 5):
            equity = row["cash_after_exit_usd"] + case / 100 * row["collateral_covered_claim_usd"]
            points.append({"Recovery": case, "Equity": equity, "Strategy": label})
    base = alt.Chart(pd.DataFrame(points)).mark_line(strokeWidth=3).encode(
        x=alt.X("Recovery:Q", title="Recovery on collateral-covered claims (%)"),
        y=alt.Y("Equity:Q", title="Terminal equity (USD)", axis=alt.Axis(format="~s")),
        color=alt.Color("Strategy:N", scale=alt.Scale(domain=list(NAMES.values()),
                        range=["#F4AA80", "#E4D188", "#71E3C0", "#82B7FC"])),
        tooltip=["Strategy:N", "Recovery:Q", alt.Tooltip("Equity:Q", format="$,.0f")],
    )
    cursor = alt.Chart(pd.DataFrame({"Recovery": [recovery]})).mark_rule(
        color="#B2C1CF", strokeDash=[5, 4]).encode(x="Recovery:Q")
    st.altair_chart((base + cursor).properties(height=300), width="stretch")


def replay_panel(published):
    st.header("Historical strategy replay")
    st.caption(f"{published['start']} to {published['terminal_exit']}  |  "
               f"{published['markets_ever_rankable']} markets ranked in the published replay  |  Ethereum USDC")
    with st.form("replay"):
        cols = st.columns(3)
        aum = cols[0].number_input("Starting capital (USDC)", 50_000, 100_000_000, 10_000_000, 50_000)
        cadence = cols[1].selectbox("Rebalance interval", [1, 3, 7, 14, 28], 2, format_func=lambda n: f"{n} days")
        min_free = cols[2].number_input("Minimum entry liquidity (USDC)", 0, 100_000_000, 2_000_000, 50_000)
        submitted = st.form_submit_button("Run replay", type="primary")
    if submitted:
        with st.spinner("Replaying the historical allocation decisions…"):
            st.session_state.replay_result = rerun(aum, cadence, min_free)
    result = st.session_state.get("replay_result", published)
    min_used = result.get("min_free_usdc", config.BACKTEST_AUM_USD / config.BACKTEST_TOP_K)
    st.caption(f"Showing {usd(result['starting_capital_usd'])} starting capital, "
               f"{result['rebalance_days']}-day rebalances and {usd(min_used)} minimum entry liquidity.")
    recovery = st.slider("Recovery on collateral-covered claims (%)", 0, 100, 50)
    st.dataframe(replay_rows(result, recovery), hide_index=True, width="stretch")
    recovery_chart(result, recovery)
    st.caption("Modeled cash uses observed free USDC. Recovery applies only to residual claims up to "
               "indexed collateral coverage. The Aave benchmark assumes exit at par.")
    st.download_button("Download replay results", json.dumps(result, indent=2), "replay.json", "application/json")


def trade_panel(published):
    st.header("Trade inspector")
    examples = published["worked_decisions"]
    names = {key: f"{row['symbol']} · {row['decision_date']}" for key, row in examples.items()}
    key = st.selectbox("Published decision", list(names), format_func=names.__getitem__)
    row = examples[key]
    st.caption(f"Latest ranking input  {row['latest_input_date']}  |  Market {row['market_id']}")
    m = st.columns(4)
    m[0].metric("Trailing supply APR", f"{row['apr']:.2%}")
    m[1].metric("Utilization", f"{row['utilization']:.2%}")
    m[2].metric("Free USDC", f"${row['free_liquidity_usd']:,.2f}")
    m[3].metric("Recent repayment", "Yes" if row["recent_repay"] else "No")
    st.dataframe([
        {"Strategy": NAMES[strategy],
         "Selected": any(market["market_id"] == row["market_id"] for market in row["selected_markets"][strategy]),
         "Net trade": usd(trade),
         "Later withdrawals": usd(row["subsequent_rebalance_withdrawals_usd"][strategy]),
         "Residual at exit": usd(row["terminal_residual_claim_usd"][strategy])}
        for strategy, trade in row["net_trade_usd"].items()
    ], hide_index=True, width="stretch")
    st.caption("Later withdrawals can include balances held before this rebalance.")
    if key == "already_illiquid":
        st.markdown("Both screens skipped a market that had spent the entire lookback near full utilization.")
    else:
        st.markdown(f"The market next reached 99% utilization on **{row['next_99pct_utilization_date']}**. "
                    "Both strategies that added exposure had exited this market by the terminal date.")
    checks = published.get("onchain_liquidity_spot_checks", {}).get(key)
    if checks:
        with st.expander("Archive RPC checks"):
            st.dataframe(checks, hide_index=True, width="stretch")


def stress_panel():
    st.header("Collateral stress sandbox")
    data = read_data("demo_stress.json.gz")
    cols = st.columns(3)
    label = cols[0].selectbox("Market", list(data["markets"]), index=list(data["markets"]).index("USDC/WBTC"))
    shock = cols[1].slider("Collateral price decline (%)", 0, 80, 25)
    kappa = cols[2].slider("Slippage multiplier", 1.0, 8.0, 2.0, 0.5)
    steps = st.radio("Shock path", [1, 6], index=1, horizontal=True,
                     format_func=lambda n: "Single gap" if n == 1 else "Six gradual steps")
    m = data["markets"][label]
    change = -shock / 100
    oracle_ratio = scenarios.market_shock_to_oracle_ratio(change, m["oracle_class"])
    res = stress.run_scenario(m["positions"], m["lltv"], m["price"], m["price"], m["borrow_assets"],
                              m["borrow_shares"], change, oracle_ratio, kappa, steps, m["fit"][:2])
    metrics = st.columns(4)
    metrics[0].metric("Liquidations", res["n_executed"])
    metrics[1].metric("Stranded positions", res["n_stranded"])
    metrics[2].metric("Realized bad debt", usd(res["realized_loss"] / 1e6))
    metrics[3].metric("Remaining shortfall", usd(res["economic_loss"] / 1e6))
    loss_bps = (res["realized_loss"] + res["economic_loss"]) / m["supply_assets"] * 10_000
    st.caption(f"Combined modeled shortfall  {loss_bps:,.1f} bps of snapshot supply  |  "
               f"Oracle move  {(oracle_ratio - 1):+.1%}  |  Block {data['block']:,}  |  Fit source  {m['fit_source']}")
    st.info("Scenario model using an aggregate slippage fit. It can extrapolate beyond measured sizes and "
            "does not model how liquidators route or stage their trades. Stranded positions were eligible "
            "for liquidation during the path and remained unexecuted.", icon=None)


def basis_panel():
    st.header("ETH basis calculator")
    st.markdown("Model annual carry on long wstETH and short ETH perps, with optional USDC borrowing.")
    cols = st.columns(3)
    leverage = cols[0].slider("Long and short notional per dollar of equity", 1.0, 5.0, 3.33, 0.01)
    funding = cols[1].number_input("Annualized funding received by short (%)", -50.0, 50.0, 3.0, 0.25)
    shock = cols[2].slider("ETH rally for short-margin stress (%)", 0, 80, 10)
    more = st.columns(3)
    stake = more[0].number_input("Annual staking yield (%)", 0.0, 20.0, 3.0, 0.25)
    borrow = more[1].number_input("Annual USDC borrowing cost (%)", 0.0, 50.0, 5.0, 0.25)
    fee = more[2].number_input("Annualized execution drag (bps of notional)", 0, 500, 8)
    carry = leverage * ((stake + funding) / 100 - fee / 10_000) - (leverage - 1) * borrow / 100
    ltv = 1 - 1 / leverage
    lltv = read_data("basis.json")["risks"]["lltv"]
    cushion = basis.depeg_to_liquidation(ltv, lltv)
    metrics = st.columns(3)
    metrics[0].metric("Modeled annual carry on equity", f"{carry:.2%}")
    metrics[1].metric("Short mark loss as share of equity", f"{basis.perp_margin_buffer_needed(leverage, shock / 100):.1%}")
    metrics[2].metric("Collateral decline to Morpho LLTV", f"{cushion:.1%}" if leverage > 1 else "No borrow")
    st.caption("Short mark loss approximates the extra cash needed to restore the original margin after the rally. "
               "Actual liquidation depends on the venue’s posted margin and maintenance requirements. "
               f"Starting LTV {ltv:.1%}, Morpho LLTV {lltv:.1%}.")


def main():
    st.set_page_config(page_title="DeFi Credit Lab", layout="wide")
    published = read_data("historical_replay.json")
    st.title("DeFi Credit Lab")
    st.markdown("**Morpho USDC research** · Lending spreads, liquidation economics and exit liquidity.")
    tabs = st.tabs(["Overview", "Strategy replay", "Trade inspector", "Collateral stress", "ETH basis"],
                   on_change="rerun")
    panels = [overview, lambda: replay_panel(published), lambda: trade_panel(published), stress_panel, basis_panel]
    for tab, panel in zip(tabs, panels):
        if tab.open:
            with tab:
                panel()


if __name__ == "__main__":
    main()
