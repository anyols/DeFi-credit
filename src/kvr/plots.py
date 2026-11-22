"""Figures for the memo (SPEC.md section 6). Each function saves a PNG to reports/figures/
and returns the path. No adjectives, no styling beyond what makes the numbers legible.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

FIGURES_DIR = Path("reports/figures")


def _save(fig, name):
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIGURES_DIR / name
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def plot_distance_to_liquidation_cdf(markets_distances, name="fig1_distance_to_liquidation_cdf.png"):
    """markets_distances: {market_label: [(price_drop_to_liq_fraction, debt_usd), ...]}."""
    fig, ax = plt.subplots(figsize=(7, 5))
    for label, points in markets_distances.items():
        if not points:
            continue
        points = sorted(points)
        drops = np.array([p[0] for p in points])
        debts = np.array([p[1] for p in points])
        cum_debt = np.cumsum(debts) / debts.sum()
        ax.plot(drops * 100, cum_debt * 100, label=label, drawstyle="steps-post")
    ax.set_xlabel("Price drop to liquidation (%)")
    ax.set_ylabel("Cumulative debt (%)")
    ax.set_title("Debt-weighted CDF of price drop to liquidation")
    ax.legend()
    ax.grid(alpha=0.3)
    return _save(fig, name)


def plot_loss_vs_shock(results_by_market, breakpoints, name="fig2_loss_vs_shock_hero.png"):
    """results_by_market: {market_label: {"gap": [scenario_result,...], "gradual": [...]}}.
    breakpoints: {market_label: breakpoint_shock_or_None}.
    """
    fig, axes = plt.subplots(1, len(results_by_market), figsize=(6 * len(results_by_market), 5), squeeze=False)
    for ax, (label, paths) in zip(axes[0], results_by_market.items()):
        for path_label, results in paths.items():
            shocks = [r["market_shock"] * 100 for r in results]
            loss_frac = [
                (r["realized_loss"] + r["economic_loss"]) / max(r["total_borrow_assets_before"], 1) * 100
                for r in results
            ]
            ax.plot(shocks, loss_frac, marker="o", label=path_label)
        bp = breakpoints.get(label)
        if bp is not None:
            ax.axvline(bp * 100, color="red", linestyle="--", label=f"breakpoint {bp:+.0%}")
        ax.set_xlabel("Market shock (%)")
        ax.set_ylabel("Loss / total borrowed (%)")
        ax.set_title(label)
        ax.legend()
        ax.grid(alpha=0.3)
    fig.suptitle("Loss vs shock, gap vs gradual, slippage-breakpoint marked")
    return _save(fig, name)


def plot_spread_waterfall(waterfalls, name="fig3_spread_waterfall.png"):
    """waterfalls: {market_label: {"spread_bps":..,"el_bps":..,"liquidity_bps":..,"residual_bps":..}}."""
    labels = list(waterfalls.keys())
    el = [waterfalls[l]["el_bps"] for l in labels]
    liq = [waterfalls[l]["liquidity_bps"] for l in labels]
    resid = [waterfalls[l]["residual_bps"] for l in labels]

    fig, ax = plt.subplots(figsize=(7, 5))
    x = np.arange(len(labels))
    ax.bar(x, el, label="EL")
    ax.bar(x, liq, bottom=el, label="Liquidity")
    bottom2 = np.array(el) + np.array(liq)
    ax.bar(x, resid, bottom=bottom2, label="Residual")
    for i, l in enumerate(labels):
        ax.scatter(i, waterfalls[l]["spread_bps"], color="black", zorder=5, label="Actual spread" if i == 0 else None)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("bps/yr")
    ax.set_title("Spread waterfall: EL + liquidity + residual vs actual spread")
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.legend()
    ax.grid(alpha=0.3)
    return _save(fig, name)


def plot_capacity_curve(curve_points, optimal_allocation_usd, market_labels, name="fig5_capacity_and_allocation.png"):
    """No vault anymore (scope change), so there's no "current allocation" to compare against --
    just the optimizer's own risk-priced split for a $1M hypothetical budget."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    budgets = [p["budget"] / 1e12 for p in curve_points]  # raw 6-decimal USDC -> $M (1e6 * 1e6)
    yields_ = [p["blended_net_yield"] * 100 for p in curve_points]
    ax1.plot(budgets, yields_, marker="o")
    ax1.set_xlabel("Capital deployed ($M)")
    ax1.set_ylabel("Blended net yield (%)")
    ax1.set_title("Capacity curve")
    ax1.grid(alpha=0.3)

    x = np.arange(len(market_labels))
    ax2.bar(x, [v / 1e9 for v in optimal_allocation_usd], 0.6, label="Risk-priced optimal ($1M budget)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(market_labels, rotation=20, ha="right")
    ax2.set_ylabel("$k")
    ax2.set_title("Risk-priced allocation")
    ax2.legend()
    ax2.grid(alpha=0.3)
    return _save(fig, name)


def plot_cross_section(rows, name="fig4_cross_section_scatter.png"):
    """rows: list of {"spread_bps", "illiq", "oracle_class", "el_bps"(maybe None)} across the
    USDC-market universe (SPEC.md M10). Two panels: spread vs illiquidity (full sample, colored
    by oracle class) and spread vs EL (the much smaller subsample where EL is estimable)."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    classes = sorted({r["oracle_class"] for r in rows})
    cmap = {c: plt.cm.tab10(i) for i, c in enumerate(classes)}

    for c in classes:
        xs = [r["illiq"] * 100 for r in rows if r["oracle_class"] == c]
        ys = [r["spread_bps"] for r in rows if r["oracle_class"] == c]
        ax1.scatter(xs, ys, label=c, color=cmap[c], alpha=0.75)
    ax1.set_xlabel("% of last 90d at >=99% utilization")
    ax1.set_ylabel("Spread vs 90d Aave USDC (bps/yr)")
    ax1.set_title(f"Full sample (n={len(rows)})")
    ax1.axhline(0, color="gray", linewidth=0.8)
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)

    with_el = [r for r in rows if r.get("el_bps") is not None]
    for c in sorted({r["oracle_class"] for r in with_el}):
        xs = [r["el_bps"] for r in with_el if r["oracle_class"] == c]
        ys = [r["spread_bps"] for r in with_el if r["oracle_class"] == c]
        ax2.scatter(xs, ys, label=c, color=cmap[c], alpha=0.75)
    ax2.set_xlabel("EL proxy (bps/yr)")
    ax2.set_ylabel("Spread vs 90d Aave USDC (bps/yr)")
    ax2.set_title(f"BTC/ETH-LST subsample (n={len(with_el)})")
    ax2.axhline(0, color="gray", linewidth=0.8)
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)
    fig.suptitle("Cross-section: spread vs illiquidity and EL, colored by oracle class")
    return _save(fig, name)


def plot_stream_case(comparison, name="fig6_stream_case.png"):
    """comparison: {label: {"hhi":.., "top1_share":.., "oracle_class":str}} for the xUSD market
    and, for reference, markets in our own universe."""
    labels = list(comparison.keys())
    hhi = [comparison[l]["hhi"] or 0 for l in labels]
    fig, ax = plt.subplots(figsize=(7, 5))
    colors = ["crimson" if "xUSD" in l else "steelblue" for l in labels]
    ax.bar(labels, hhi, color=colors)
    for i, l in enumerate(labels):
        ax.annotate(comparison[l]["oracle_class"], (i, hhi[i]), ha="center", va="bottom")
    ax.set_ylabel("Borrower HHI (0-1)")
    ax.set_title("Stream/xUSD case: concentration + oracle class vs our own universe's markets")
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.grid(alpha=0.3)
    return _save(fig, name)


def plot_liq_edge_profitability(summaries, name="fig7_liq_edge_profitability.png"):
    """Task 4. summaries: {label: {"profitable_share_dex":.., "profitable_share_mm":..}}."""
    labels = [l for l, s in summaries.items() if s["profitable_share_dex"] is not None or s["profitable_share_mm"] is not None]
    fig, ax = plt.subplots(figsize=(8, 5))
    if labels:
        x = np.arange(len(labels))
        width = 0.35
        ax.bar(x - width / 2, [(summaries[l]["profitable_share_dex"] or 0) * 100 for l in labels], width, label="DEX execution")
        ax.bar(x + width / 2, [(summaries[l]["profitable_share_mm"] or 0) * 100 for l in labels], width, label="MM execution (assumed spread)")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("Profitable share of real liquidations (%)")
    ax.set_title("Liquidator profitability over the real sample, DEX vs assumed MM execution")
    ax.legend()
    ax.grid(alpha=0.3)
    return _save(fig, name)


def plot_liq_edge_delay(delay_samples_by_market, sample_cap, name="fig8_liq_edge_delay.png"):
    """Task 4. delay_samples_by_market: {label: [hours_or_None, ...]} (a bounded sample per
    market, see config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET -- sample_cap is only for the title)."""
    data, labels = [], []
    for label, samples in delay_samples_by_market.items():
        valid = [d for d in samples if d is not None]
        if valid:
            data.append(valid)
            labels.append(label)
    fig, ax = plt.subplots(figsize=(8, 5))
    if data:
        ax.boxplot(data, tick_labels=labels)
    ax.set_ylabel("Hours between oracle breach and actual liquidation")
    ax.set_title(f"Delay to liquidation (sampled, up to {sample_cap}/market)")
    ax.tick_params(axis="x", rotation=20)
    ax.grid(alpha=0.3)
    return _save(fig, name)


def plot_liq_edge_left_on_table(summaries, name="fig9_liq_edge_left_on_table.png"):
    """Task 4. summaries: {label: {"total_left_on_table_usd":..}} -- ground-truth badDebtAssets
    summed over the real sample, not a modeled estimate."""
    labels = list(summaries.keys())
    left = [summaries[l]["total_left_on_table_usd"] for l in labels]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(labels, left)
    ax.set_ylabel("Left on the table -- unrecovered bad debt (USD)")
    ax.set_title("Bad debt realized per collateral, last 24 months")
    ax.tick_params(axis="x", rotation=20)
    ax.grid(alpha=0.3)
    return _save(fig, name)
