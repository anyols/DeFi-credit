

import numpy as np
from scipy.optimize import minimize

from . import irm

WAD = 10**18
SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def income_rate(x, supply_assets, borrow_assets, rate_at_target, fee_frac=0.0):
    """Marginal annual income (in loan-token units) from depositing x more into this market,
    holding borrow demand (Bor_i) fixed -- the extra deposit only dilutes utilization down.
    """
    new_supply = supply_assets + x
    if new_supply == 0 or borrow_assets == 0:
        return 0.0
    u = irm.utilization(borrow_assets, new_supply)
    borrow_rate_per_second, _end_rt = irm.borrow_rate(u, rate_at_target, 0)
    borrow_apy = irm.apy_from_per_second_rate(borrow_rate_per_second)
    our_share = x / new_supply
    return our_share * borrow_assets * borrow_apy * (1 - fee_frac)


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


def _objective(x, markets, el_rate_default):
    total = 0.0
    for xi, m in zip(x, markets):
        el_rate = m.get("el_rate", el_rate_default)
        total += income_rate(xi, m["supply_assets"], m["borrow_assets"], m["rate_at_target"], m.get("fee", 0.0)) - el_rate * xi
    return -total  # minimize() minimizes; we want to maximize total


def slsqp_check(markets, budget, el_rate_default=0.0, n_starts=5, seed=0):
    """Cross-checks greedy_water_fill with scipy.optimize.minimize(SLSQP) from several starting
    points (zeros, even split, and random feasible points). Returns the best SLSQP solution
    found (by objective value) alongside the greedy solution, for comparison -- not a
    replacement, since SLSQP can land in a different local optimum depending on the start.

    Known divergence, first found against a market sitting at extreme utilization long enough
    that its rateAtTarget had drifted to AdaptiveCurveIrm's own MAX_RATE_AT_TARGET ceiling (a
    market pinned at ~100% utilization for an extended stretch -- see spread.py's findings for
    the general pattern): income_rate(x) spikes near x->0 for any such market, because SPEC.md
    M11's own "hold Bor_i fixed" assumption breaks down exactly where utilization is already
    pinned near 100% -- real borrow demand is rate-sensitive, so no borrower actually keeps
    paying as the rate implied by a tiny marginal deposit spikes; this isn't capturable income,
    it's a modeling artifact. SLSQP's gradient search can get pulled toward this spike from some
    starting points; greedy's coarse $50k step size never samples a small enough x to find it,
    which is incidentally more robust here, not because greedy is the better algorithm in
    general. Re-check this docstring's claim whenever a market in the live universe is this
    extreme -- the numbers here are illustrative of the shape of the bug, not a fixed example.
    """
    n = len(markets)
    caps = [m.get("cap", budget) for m in markets]
    bounds = [(0, min(cap, budget)) for cap in caps]
    constraints = [{"type": "ineq", "fun": lambda x: budget - sum(x)}]

    rng = np.random.default_rng(seed)
    starts = [np.zeros(n), np.full(n, budget / n)]
    for _ in range(n_starts - len(starts)):
        raw = rng.random(n)
        raw = raw / raw.sum() * budget
        starts.append(np.minimum(raw, [b[1] for b in bounds]))

    best = None
    for x0 in starts:
        result = minimize(_objective, x0, args=(markets, el_rate_default), method="SLSQP", bounds=bounds, constraints=constraints)
        if result.success and (best is None or result.fun < best.fun):
            best = result
    if best is None:
        return None

    greedy_alloc = greedy_water_fill(markets, budget, el_rate_default=el_rate_default)
    greedy_objective = -_objective(greedy_alloc, markets, el_rate_default)
    slsqp_objective = -best.fun
    return {
        "slsqp_allocation": [max(0.0, v) for v in best.x],
        "slsqp_objective": slsqp_objective,
        "greedy_allocation": greedy_alloc,
        "greedy_objective": greedy_objective,
        "rel_diff": abs(slsqp_objective - greedy_objective) / max(abs(slsqp_objective), 1e-12),
    }


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
