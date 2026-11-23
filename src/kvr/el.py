


def marginal_frequencies(shocks_sorted_by_severity, lambda_fn):
    """shocks_sorted_by_severity: least severe first (e.g. -0.10, -0.15, ..., -0.60).
    Returns {shock: marginal_annual_frequency} so each historical event is counted in exactly
    one bucket (the bucket for its own severity), not every bucket it would also exceed.
    """
    freqs = {}
    cum = [lambda_fn(abs(s)) for s in shocks_sorted_by_severity]
    for i, s in enumerate(shocks_sorted_by_severity):
        next_cum = cum[i + 1] if i + 1 < len(cum) else 0.0
        freqs[s] = max(0.0, cum[i] - next_cum)
    return freqs


def expected_loss_bps(scenario_results, total_supply_assets, lambda_fn):
    """scenario_results: list of stress.run_scenario() outputs for one market, one kappa, one
    path, covering the full shock grid (least-severe shock first). Returns EL in bps/yr, or
    None if lambda_fn is None (SPEC.md: 'not estimable, judgment only' -- never invent a number).
    """
    if lambda_fn is None or total_supply_assets == 0:
        return None
    shocks = [r["market_shock"] for r in scenario_results]
    freqs = marginal_frequencies(shocks, lambda_fn)
    el_frac = 0.0
    for r in scenario_results:
        loss_frac = (r["realized_loss"] + r["economic_loss"]) / total_supply_assets
        el_frac += freqs[r["market_shock"]] * loss_frac
    return el_frac * 10_000


def el_range(scenario_results_base_kappa, scenario_results_stressed_kappa, total_supply_assets, lambda_fn):
    """The two scenario_results lists must cover the same shock grid, differing only in kappa."""
    base = expected_loss_bps(scenario_results_base_kappa, total_supply_assets, lambda_fn)
    stressed = expected_loss_bps(scenario_results_stressed_kappa, total_supply_assets, lambda_fn)
    if base is None or stressed is None:
        return None
    return (min(base, stressed), max(base, stressed))
