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
from kvr import basis, historical_replay, scenarios, spread_replay, stress

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
        st.markdown(f"**{data['total_events']} historical liquidations.** The chart uses execution-block "
                    "oracle marks, whole-transaction gas and assumed exit charges. Actual sale prices "
                    "and realized liquidator P&L are unavailable.")
        report = read_data("report_results.json")
        st.markdown(f"RLP recorded **{millions(data['rlp_case_study']['bad_debt_usd'])} in bad debt**. "
                    f"The five persistently illiquid markets had "
                    f"**{report['zombie_free_usdc_total']:,.0f} USDC in free liquidity** at the snapshot.")
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
        equity = historical_replay.recovery_equity(row, recovery)
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
            equity = historical_replay.recovery_equity(row, case)
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
    st.markdown("**Greedy** ranks trailing APR. **Repayment screen** also rejects persistently "
                "near-full markets without a recent repayment. **Liquidity screen** adds a "
                "minimum amount of free USDC before entry.")
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
    st.caption("Morpho exits use observed free USDC. Recovery applies to residual claims up to indexed "
               "collateral coverage. Aave assumes full exit at par and pays $50 each for entry and exit.")
    fraction = historical_replay.aave_exit_fraction_to_match_best_morpho(result["table"], recovery)
    st.caption(f"At this recovery rate Aave ties the best Morpho rule at {fraction:.1%} "
               "of its modeled terminal value.")
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


def spread_panel():
    st.header("Liquidity-adjusted spread")
    st.markdown("Start in Aave. Rotate into eligible Morpho markets when one rebalance period of "
                "estimated excess carry covers the modeled round-trip gas.")
    scenario = st.radio("Aave withdrawal capacity", ["Full exit", "$1M per withdrawal"],
                        horizontal=True)
    key = "base" if scenario == "Full exit" else "aave_capacity_stress"
    saved = read_data("spread_replay.json")
    with st.form("spread_assumptions"):
        c1, c2 = st.columns(2)
        capital = c1.selectbox("Starting capital", (10_000_000, 1_000_000), format_func=usd)
        liquidity_multiple = c2.selectbox("Minimum free USDC", (1, 2), format_func=lambda n: f"{n}× initial sleeve")
        submit = st.form_submit_button("Replay with these assumptions")
    if submit:
        st.session_state.spread_active_parameters = (capital, liquidity_multiple)
    capital, liquidity_multiple = st.session_state.get("spread_active_parameters", (10_000_000, 1))
    if (capital, liquidity_multiple) == (10_000_000, 1):
        result = saved[key]
    else:
        result = run_spread_demo(capital, liquidity_multiple, key)
    st.caption(f"{result['start']} to {result['terminal_exit']} · {usd(result['starting_capital_usd'])} "
               "starting capital · Retrospective evaluation")
    names = {**NAMES, spread_replay.SPREAD: "Adjusted spread"}
    st.dataframe([
        {"Strategy": names[name], "Modeled exit cash": usd(row["cash_after_exit_usd"]),
         "Equity at 25% Morpho recovery": usd(row["equity_by_recovery_pct_usd"]["25"]),
         "Equity at 50% Morpho recovery": usd(row["equity_by_recovery_pct_usd"]["50"]),
         "Equity at 100% Morpho recovery": usd(row["equity_by_recovery_pct_usd"]["100"]),
         "Residual face claims": usd(row["residual_claim_usd"]),
         "Gross turnover": usd(row["gross_rebalance_turnover_usd"]),
         "Recovery-mark drawdown": f"{row['max_drawdown_at_50pct_recovery']:.1%}"}
        for name, row in result["table"].items()
    ], hide_index=True, width="stretch")
    st.caption("Aave capacity is a scenario assumption. Unwithdrawn Aave claims remain marked at par; "
               "the 50% recovery rate applies only to collateral-covered Morpho claims.")
    with st.expander("Allocation and risk diagnostics"):
        st.dataframe([
            {"Strategy": names[name], "Annualized at 50% recovery": f"{row['annualized_at_50pct_recovery']:.1%}",
             "Transfers before terminal exit": row["rebalance_transactions"],
             "Mean Aave share": f"{row['mean_aave_fraction']:.1%}",
             "Mean Morpho share": f"{row['mean_morpho_fraction']:.1%}",
             "Days with near-full market exposure": row["days_with_distressed_exposure"],
             "Days with claims awaiting exit": row["days_with_exit_requested_claim"]}
            for name, row in result["table"].items()
        ], hide_index=True, width="stretch")
    paths = [{**point, "Strategy": names[name]} for name, points in result["paths"].items() for point in points]
    curves = pd.DataFrame(paths).pivot(index="date", columns="Strategy", values="marked_equity_usd") / 1e6
    curves.index = pd.to_datetime(curves.index)
    st.line_chart(curves, y_label="Equity at 50% Morpho recovery ($M)")
    allocation = pd.DataFrame(result["paths"][spread_replay.SPREAD]).set_index("date")
    allocation.index = pd.to_datetime(allocation.index)
    st.line_chart((allocation[["aave_fraction", "morpho_fraction", "exit_requested_fraction"]]
                   .rename(columns={"aave_fraction": "Aave", "morpho_fraction": "Morpho",
                                    "exit_requested_fraction": "Awaiting Morpho exit"})) * 100,
                  y_label="Share of nominal claims (%)")
    choices = result["worked_decisions"]
    if choices:
        action = st.selectbox("Inspect a replayed decision", list(choices),
                              format_func=lambda key: key.replace("_", " ").title())
        row = choices[action]
        signal, observed = row["signal"], row["observed"]
        st.markdown(f"**{row['symbol']} · {row['date']}**")
        if observed:
            st.write(f"Previous-day free USDC {usd(observed['free_liquidity_usd'])}. "
                     f"Repayment screen {'passed' if observed['repay_screen_pass'] else 'failed'}.")
        if signal:
            st.write(f"Diluted annual yield advantage {signal['annual_yield_advantage']:.2%}. "
                     f"Expected excess carry after entry hurdle {usd(signal['net_spread_usd'])}.")
        else:
            st.write("The market no longer passed the entry risk gates.")
        st.write(f"Net fill {usd(row['trade_usdc'])}. Claim still held after the rebalance "
                 f"{usd(row['after_usdc'])}. Later withdrawals {usd(row['later_withdrawals_usdc'])}.")


@st.cache_data(max_entries=8, show_spinner="Running spread replay...")
def run_spread_demo(capital, liquidity_multiple, key):
    cap = 1_000_000 if key == "aave_capacity_stress" else None
    return spread_replay.run(read_data("demo_replay.json.gz"), aum=capital,
                             min_free=liquidity_multiple * capital / config.BACKTEST_TOP_K,
                             aave_capacity=cap)


def main():
    st.set_page_config(page_title="DeFi Credit Lab", layout="wide")
    published = read_data("historical_replay.json")
    st.title("DeFi Credit Lab")
    st.markdown("**Morpho USDC research** · Lending spreads, liquidation economics and exit liquidity.")
    tabs = st.tabs(["Overview", "Strategy replay", "Spread signal", "Trade inspector", "Collateral stress", "ETH basis"],
                   on_change="rerun")
    panels = [overview, lambda: replay_panel(published), spread_panel,
              lambda: trade_panel(published), stress_panel, basis_panel]
    for tab, panel in zip(tabs, panels):
        if tab.open:
            with tab:
                panel()


if __name__ == "__main__":
    main()
