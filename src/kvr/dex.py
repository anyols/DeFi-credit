"""M6: DEX depth via Uniswap V3 QuoterV2 (eth_call simulation, no real swap) + slippage fit.

QuoterV2 address (0x61fFE014bA17989E743c5F6cB21bF9697530B21e) verified to have deployed code at
config.SNAPSHOT_BLOCK via eth_getCode, and the quoteExactInputSingle struct layout verified
against Uniswap/v3-periphery's IQuoterV2.sol.
"""

import numpy as np

from . import chain

QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
FEE_TIERS = (100, 500, 3000, 10000)

QUOTE_SIZES_TOKEN = (50_000, 250_000, 1_000_000, 5_000_000, 20_000_000, 50_000_000)  # USD-ish units
DEPTH_STRESS_KAPPA = (1, 2, 4)  # see SPEC.md M6 -- an assumption, not measured, labeled as one


def quote_exact_input_single(token_in, token_out, amount_in, fee, block):
    params = (token_in, token_out, amount_in, fee, 0)
    amount_out, _sqrt_after, _ticks, _gas = chain.call(QUOTER_V2, "quoteExactInputSingle", [params], block)
    return amount_out


def best_quote(token_in, token_out, amount_in, block):
    """Try every fee tier, keep whichever pool returns the most output (deepest/cheapest route).
    Returns (fee, amount_out), or None if no pool exists for this pair at any tier.
    """
    best = None
    for fee in FEE_TIERS:
        try:
            out = quote_exact_input_single(token_in, token_out, amount_in, fee, block)
        except Exception:
            continue
        if out > 0 and (best is None or out > best[1]):
            best = (fee, out)
    return best


def quote_curve(token_in, token_out, sizes_raw, block):
    """sizes_raw: raw token_in amounts (already decimal-adjusted by the caller). Returns a list
    of (amount_in, amount_out, fee_tier) for every size a pool could be found for.
    """
    points = []
    for amount_in in sizes_raw:
        result = best_quote(token_in, token_out, amount_in, block)
        if result is not None:
            fee, amount_out = result
            points.append((amount_in, amount_out, fee))
    return points


def fit_slippage(points):
    """Fits slip(q) = a * q^b in log-log space, where slip is price impact relative to the
    smallest quoted size's effective rate (amount_out/amount_in), and q is amount_in. Expect
    b ~= 0.5 (SPEC.md M6). Returns (a, b, depth_cap) or None if fewer than 2 usable points.

    depth_cap is the largest quoted size before `amount_out` stops meaningfully increasing with
    size (<1% marginal gain over the previous point) -- real pools run out of depth well inside
    the spec's 50M quote range, and fitting the clean region only (dropping sizes past the cliff)
    is what keeps the power-law fit honest instead of silently extrapolating past it.
    """
    usable = sorted(((q, out / q) for q, out, _fee in points if q > 0))
    if len(usable) < 2:
        return None

    depth_cap = usable[-1][0]
    clean = [usable[0]]
    for i in range(1, len(usable)):
        prev_out = usable[i - 1][0] * usable[i - 1][1]
        this_out = usable[i][0] * usable[i][1]
        if prev_out > 0 and (this_out - prev_out) / prev_out < 0.01:
            depth_cap = usable[i - 1][0]
            break
        clean.append(usable[i])

    if len(clean) < 3:  # need the baseline plus at least 2 slip points
        return None
    baseline_rate = clean[0][1]
    slips = [(q, max(1e-12, 1 - rate / baseline_rate)) for q, rate in clean[1:]]
    log_q = np.log([q for q, _ in slips])
    log_slip = np.log([s for _, s in slips])
    b, log_a = np.polyfit(log_q, log_slip, 1)
    return float(np.exp(log_a)), float(b), depth_cap


def predict_slippage(a, b, q):
    return a * (q**b)


def stressed_slippage(a, b, q, kappa):
    """SPEC.md M6: under stress, depth shrinks: slip_s(q) = kappa * slip(q). Label it an
    assumption, not a measurement -- there is no live data source for depth under stress."""
    return kappa * predict_slippage(a, b, q)
