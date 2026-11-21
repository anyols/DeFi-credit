"""M11: risk-priced allocation. Scope note (SPEC.md section 7's own cut list, applied given
time constraints): the SLSQP cross-check is cut, keeping only the greedy water-filling solver
it explicitly allows dropping.

income_i(x) = x/(S_i+x) * Bor_i * r_borrow(Bor_i/(S_i+x)) * (1-fee_i)
maximize sum_i [income_i(x_i) - EL_i * x_i], subject to caps and sum(x_i) <= B.
"""

from . import irm

WAD = 10**18
SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def income_rate(x, supply_assets, borrow_assets, rate_at_target, fee_frac=0.0):
    """Marginal annual income (in loan-token units) from depositing x more into this market,
    holding borrow demand (Bor_i) fixed -- the vault's deposit only dilutes utilization down.
    """
    new_supply = supply_assets + x
    if new_supply == 0 or borrow_assets == 0:
        return 0.0
    u = irm.utilization(borrow_assets, new_supply)
    borrow_rate_per_second, _end_rt = irm.borrow_rate(u, rate_at_target, 0)
    borrow_apy = irm.apy_from_per_second_rate(borrow_rate_per_second)
    vault_share = x / new_supply
    return vault_share * borrow_assets * borrow_apy * (1 - fee_frac)


def greedy_water_fill(markets, budget, step=50_000 * 10**6, el_rate_default=0.0):
    """markets: list of dicts with supply_assets, borrow_assets, rate_at_target, fee, cap,
    el_rate (annual EL as a fraction of allocation, e.g. 0.001 for 10bps/yr -- default 0 if not
    estimable, per SPEC.md's own rule not to invent a number, labeled via el_rate_default).
    Returns {market_index: allocated_x}, filling in $step increments by marginal risk-adjusted
    yield (income'(x) - el_rate*x), same algorithm whether or not el_rate is known for a market.
    """
    alloc = [0] * len(markets)
    remaining = budget
    while remaining > 0:
        best_i, best_marginal = None, -1
        for i, m in enumerate(markets):
            if alloc[i] + step > m.get("cap", float("inf")):
                continue
            x = alloc[i]
            el_rate = m.get("el_rate", el_rate_default)
            income_before = income_rate(x, m["supply_assets"], m["borrow_assets"], m["rate_at_target"], m.get("fee", 0.0))
            income_after = income_rate(x + step, m["supply_assets"], m["borrow_assets"], m["rate_at_target"], m.get("fee", 0.0))
            marginal_income = income_after - income_before
            marginal_risk_adjusted = marginal_income - el_rate * step
            if marginal_risk_adjusted > best_marginal:
                best_marginal, best_i = marginal_risk_adjusted, i
        if best_i is None or best_marginal <= 0:
            break
        this_step = min(step, remaining)
        alloc[best_i] += this_step
        remaining -= this_step
    return alloc


def capacity_curve(markets, budgets, step=50_000 * 10**6, el_rate_default=0.0):
    """Blended net yield vs total deployable capital B, for B in `budgets`."""
    points = []
    for b in budgets:
        alloc = greedy_water_fill(markets, b, step, el_rate_default)
        total_income = sum(
            income_rate(alloc[i], m["supply_assets"], m["borrow_assets"], m["rate_at_target"], m.get("fee", 0.0))
            for i, m in enumerate(markets)
        )
        blended_yield = total_income / b if b > 0 else 0.0
        points.append({"budget": b, "allocation": list(alloc), "blended_net_yield": blended_yield})
    return points
