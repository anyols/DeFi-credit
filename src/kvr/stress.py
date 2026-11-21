"""M8: the liquidation loop -- the core insight of the whole project (SPEC.md).

For each scenario, walks the shock path step by step: at each step's oracle price, finds every
liquidatable position, sizes the aggregate seize notional for that step (slippage depends on the
TOTAL being dumped, not any one position), and only executes a liquidation if it's still
profitable after that slippage. Positions nobody finds profitable to liquidate are "stranded" --
the breakpoint shock where stranding starts is the headline number.
"""

from . import dex
from . import morpho_math as mm

DEFAULT_GAS_COST_LOAN_UNITS = 50 * 10**6  # ~$50 in a 6-decimal loan token; label it an assumption


def positions_at_risk(positions, lltv, oracle_price, total_borrow_assets, total_borrow_shares):
    out = []
    for p in positions:
        borrowed = mm.borrowed(p["borrow_shares"], total_borrow_assets, total_borrow_shares)
        if borrowed == 0:
            continue
        max_b = mm.max_borrow(p["collateral"], oracle_price, lltv)
        out.append({**p, "borrowed": borrowed, "max_borrow": max_b, "liquidatable": borrowed > max_b})
    return out


def run_path_step(
    positions,
    lltv,
    oracle_price,
    market_price,
    total_borrow_assets,
    total_borrow_shares,
    slip_fn,
    gas_cost_loan_units=DEFAULT_GAS_COST_LOAN_UNITS,
):
    """One path step at a fixed (oracle_price, market_price). slip_fn(notional) -> fraction in [0,1).

    Simplification, labeled as one: liquidations within a step are applied sequentially against
    running totals, using one aggregate slippage figure computed from the naive (pre-slippage)
    seize sizes of every liquidatable position -- not a full iterative re-solve. Good enough to
    locate the breakpoint; a tighter simulation would re-price slippage after each execution.
    """
    liquidatable = [p for p in positions_at_risk(positions, lltv, oracle_price, total_borrow_assets, total_borrow_shares) if p["liquidatable"]]

    lif = mm.liquidation_incentive_factor(lltv)
    naive_seize = {}
    total_notional = 0
    for p in liquidatable:
        seize_full = mm.seized_for_repaid_shares(p["borrow_shares"], oracle_price, lif, total_borrow_assets, total_borrow_shares)
        seize = min(seize_full, p["collateral"])
        naive_seize[p["user"]] = seize
        total_notional += seize
    slip = min(slip_fn(total_notional), 1.0) if total_notional > 0 else 0.0

    executed, stranded = [], []
    bad_debt_assets_total = 0
    tb_assets, tb_shares = total_borrow_assets, total_borrow_shares
    for p in liquidatable:
        seize = naive_seize[p["user"]]
        if seize == 0:
            stranded.append({**p, "reason": "zero seizable collateral"})
            continue
        result = mm.apply_liquidation(p["collateral"], p["borrow_shares"], oracle_price, lltv, tb_assets, tb_shares, seized_assets=seize)
        _new_collateral, _new_borrow_shares, seized, repaid_assets, _bd_shares, bad_debt_assets, new_tba, new_tbs = result

        proceeds = int(seized * market_price // 10**36 * (1 - slip))
        profit = proceeds - repaid_assets - gas_cost_loan_units
        if profit >= 0:
            executed.append({**p, "seized": seized, "repaid_assets": repaid_assets, "bad_debt_assets": bad_debt_assets, "profit": profit})
            bad_debt_assets_total += bad_debt_assets
            tb_assets, tb_shares = new_tba, new_tbs
        else:
            stranded.append({**p, "would_be_profit": profit, "slippage": slip})

    return {
        "executed": executed,
        "stranded": stranded,
        "bad_debt_assets": bad_debt_assets_total,
        "total_seize_notional": total_notional,
        "slippage": slip,
        "total_borrow_assets": tb_assets,
        "total_borrow_shares": tb_shares,
    }


def run_scenario(
    positions,
    lltv,
    base_oracle_price,
    base_market_price,
    total_borrow_assets,
    total_borrow_shares,
    market_shock,
    oracle_shock_ratio,
    kappa,
    path_steps,
    slip_ab,
    gas_cost_loan_units=DEFAULT_GAS_COST_LOAN_UNITS,
):
    """slip_ab: (a, b) from dex.fit_slippage. oracle_shock_ratio: the final oracle price ratio
    (1.0 for a FIXED/depeg-only shock, 1+market_shock for a MARKET oracle seeing the real move).
    """
    market_ratio_final = 1 + market_shock
    remaining = {p["user"]: dict(p) for p in positions}
    tb_assets, tb_shares = total_borrow_assets, total_borrow_shares
    realized_loss = 0
    n_executed = 0
    oracle_price = base_oracle_price
    market_price = base_market_price

    def slip_s(q):
        a, b = slip_ab
        return dex.stressed_slippage(a, b, q, kappa) if q > 0 else 0.0

    for step in range(1, path_steps + 1):
        frac = step / path_steps
        oracle_price = int(base_oracle_price * (1 + (oracle_shock_ratio - 1) * frac))
        market_price = int(base_market_price * (1 + (market_ratio_final - 1) * frac))
        result = run_path_step(list(remaining.values()), lltv, oracle_price, market_price, tb_assets, tb_shares, slip_s, gas_cost_loan_units)
        for ex in result["executed"]:
            remaining.pop(ex["user"], None)
        realized_loss += result["bad_debt_assets"]
        n_executed += len(result["executed"])
        tb_assets, tb_shares = result["total_borrow_assets"], result["total_borrow_shares"]

    economic_loss = 0
    for p in remaining.values():
        borrowed = mm.borrowed(p["borrow_shares"], tb_assets, tb_shares)
        collateral_value = p["collateral"] * market_price // 10**36
        economic_loss += max(0, borrowed - collateral_value)

    return {
        "market_shock": market_shock,
        "kappa": kappa,
        "path_steps": path_steps,
        "realized_loss": realized_loss,
        "economic_loss": economic_loss,
        "n_executed": n_executed,
        "n_stranded": len(remaining),
        "total_borrow_assets_before": total_borrow_assets,
    }


def find_breakpoint_shock(
    positions, lltv, base_oracle_price, base_market_price, total_borrow_assets, total_borrow_shares,
    shocks, oracle_class, is_depeg_asset, kappa, slip_ab, path_steps=1,
):
    """The core insight (SPEC.md M8): the smallest-magnitude market_shock in `shocks` (sorted by
    magnitude, least severe first) at which at least one liquidatable position goes stranded.
    Returns the shock value, or None if nothing in the grid strands a position.
    """
    for m in sorted(shocks, key=abs):
        oracle_ratio = 1.0 if oracle_class == "FIXED" or (oracle_class == "EXCHANGE_RATE" and is_depeg_asset) else 1 + m
        result = run_scenario(
            positions, lltv, base_oracle_price, base_market_price, total_borrow_assets, total_borrow_shares,
            m, oracle_ratio, kappa, path_steps, slip_ab,
        )
        if result["n_stranded"] > 0:
            return m
    return None


def liquidity_stress(total_supply_assets, total_borrow_assets):
    """SPEC.md M8: withdrawable = totalSupply - totalBorrow. Fraction of the pool that could
    exit right now if utilization went to 100%."""
    withdrawable = max(0, total_supply_assets - total_borrow_assets)
    frac = withdrawable / total_supply_assets if total_supply_assets > 0 else 0.0
    return {"withdrawable": withdrawable, "withdrawable_fraction": frac}
