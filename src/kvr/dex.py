

import time

import numpy as np
import requests

from . import chain

QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
FEE_TIERS = (100, 500, 3000, 10000)
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WBTC = "0x2260FAC5e5542a773Aa44fBCfeDf7C193bc2C599"
ROUTING_INTERMEDIATES = (WETH, WBTC)  # Task 3 addendum: route thin/absent direct pairs via these

QUOTE_SIZES_TOKEN = (50_000, 250_000, 1_000_000, 5_000_000, 20_000_000, 50_000_000)  # USD-ish units
DEPTH_STRESS_KAPPA = (1, 2, 4)  # see SPEC.md M6 -- an assumption, not measured, labeled as one

PARASWAP_PRICES_URL = "https://apiv5.paraswap.io/prices"  # no API key required -- verified by hand


def quote_exact_input_single(token_in, token_out, amount_in, fee, block):
    params = (token_in, token_out, amount_in, fee, 0)
    amount_out, _sqrt_after, _ticks, _gas = chain.call(QUOTER_V2, "quoteExactInputSingle", [params], block)
    return amount_out


def best_quote(token_in, token_out, amount_in, block):
    """Try every fee tier on the DIRECT pair only, keep whichever pool returns the most output.
    Returns (fee, amount_out), or None if no direct pool exists at any tier. Most callers want
    best_route instead (direct + multi-hop); this is kept for the direct-pair-only case and as
    best_route's building block.
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


def _encode_path(tokens, fees):
    """abi.encodePacked(token0, fee0, token1, fee1, ..., tokenN) -- NOT standard ABI encoding
    (no padding, no offsets): 20-byte address + 3-byte big-endian fee, repeated, per
    Uniswap/v3-periphery's path encoding convention for QuoterV2.quoteExactInput."""
    parts = []
    for token, fee in zip(tokens, fees):
        parts.append(bytes.fromhex(token[2:]))
        parts.append(fee.to_bytes(3, "big"))
    parts.append(bytes.fromhex(tokens[-1][2:]))
    return b"".join(parts)


def quote_exact_input_multihop(tokens, fees, amount_in, block):
    """tokens: [token_in, intermediate, ..., token_out], fees: one per hop (len(tokens)-1)."""
    path = _encode_path(tokens, fees)
    amount_out, *_rest = chain.call(QUOTER_V2, "quoteExactInput", [path, amount_in], block)
    return amount_out


def _live_fee_tiers(token_in, token_out, probe_amount, block):
    """Which of FEE_TIERS has ANY pool for this exact pair, probed once at probe_amount. Used to
    avoid wasting a 2-hop quoteExactInput call (no cheaper than a 1-hop one) on a fee-tier
    combination where one leg alone already has no pool -- in practice most pairs concentrate
    liquidity in 1-2 of the 4 tiers, so this cuts the 2-hop search space sharply."""
    live = []
    for fee in FEE_TIERS:
        try:
            if quote_exact_input_single(token_in, token_out, probe_amount, fee, block) > 0:
                live.append(fee)
        except Exception:
            continue
    return live


def best_route(token_in, token_out, amount_in, block, intermediates=ROUTING_INTERMEDIATES, live_legs=None):
    """Direct pair (every fee tier) plus a 2-hop route through each intermediate, keeping
    whichever returns the most output. Returns (route_label, amount_out), or None if nothing
    quotes at all. route_label is "direct:<fee>" or "<intermediate>:<fee0>,<fee1>" -- purely
    descriptive, never parsed back (fit_slippage discards it like the old single fee tier).

    live_legs: optional {intermediate: (leg1_fees, leg2_fees)} from quote_curve, precomputed
    once per pair and reused across every quote size -- pool existence doesn't change with
    amount_in, only whether a call reverts from exhausted liquidity at a given size, so this
    existence check is safe to compute once. Falls back to probing at amount_in if not given.
    """
    best = None
    direct = best_quote(token_in, token_out, amount_in, block)
    if direct is not None:
        best = (f"direct:{direct[0]}", direct[1])
    for intermediate in intermediates:
        if intermediate.lower() in (token_in.lower(), token_out.lower()):
            continue
        if live_legs is not None and intermediate in live_legs:
            leg1_fees, leg2_fees = live_legs[intermediate]
        else:
            leg1_fees = _live_fee_tiers(token_in, intermediate, amount_in, block)
            leg2_fees = _live_fee_tiers(intermediate, token_out, amount_in, block)
        for fee0 in leg1_fees:
            for fee1 in leg2_fees:
                try:
                    out = quote_exact_input_multihop([token_in, intermediate, token_out], [fee0, fee1], amount_in, block)
                except Exception:
                    continue
                if out > 0 and (best is None or out > best[1]):
                    best = (f"{intermediate[:8]}:{fee0},{fee1}", out)
    return best


def quote_curve(token_in, token_out, sizes_raw, block, intermediates=ROUTING_INTERMEDIATES):
    """sizes_raw: raw token_in amounts (already decimal-adjusted by the caller). Returns a list
    of (amount_in, amount_out, route_label) for every size a route (direct or multi-hop) could
    be found for. Probes each intermediate's live fee tiers once (at the smallest size) and
    reuses that across all sizes -- see best_route's live_legs docstring.
    """
    live_legs = {}
    if sizes_raw:
        probe_amount = min(sizes_raw)
        for intermediate in intermediates:
            if intermediate.lower() in (token_in.lower(), token_out.lower()):
                continue
            live_legs[intermediate] = (
                _live_fee_tiers(token_in, intermediate, probe_amount, block),
                _live_fee_tiers(intermediate, token_out, probe_amount, block),
            )
    points = []
    for amount_in in sizes_raw:
        result = best_route(token_in, token_out, amount_in, block, intermediates=intermediates, live_legs=live_legs)
        if result is not None:
            route_label, amount_out = result
            points.append((amount_in, amount_out, route_label))
    return points


def aggregator_quote(token_in, token_out, amount_in, in_decimals, out_decimals=6):
    """ParaSwap /prices (apiv5.paraswap.io), verified by hand to need no API key. Returns the
    destAmount (raw out-token units) for a SELL of amount_in, or None on any failure/zero quote
    -- an aggregator routing failure is not fatal here, it just means this source has no data.
    """
    try:
        r = requests.get(
            PARASWAP_PRICES_URL,
            params={
                "srcToken": token_in,
                "destToken": token_out,
                "amount": amount_in,
                "side": "SELL",
                "network": 1,
                "srcDecimals": in_decimals,
                "destDecimals": out_decimals,
            },
            timeout=20,
        )
        r.raise_for_status()
        out = int(r.json()["priceRoute"]["destAmount"])
        return out if out > 0 else None
    except Exception:
        return None


def aggregator_quote_curve(token_in, token_out, sizes_raw, in_decimals, out_decimals=6, pause=0.2):
    """Same shape as quote_curve, but via the aggregator instead of direct on-chain quoting."""
    points = []
    for amount_in in sizes_raw:
        out = aggregator_quote(token_in, token_out, amount_in, in_decimals, out_decimals)
        if out is not None:
            points.append((amount_in, out, "aggregator"))
        time.sleep(pause)  # be a reasonable citizen of a free, no-key public API
    return points


def deeper_fit(onchain_points, aggregator_points):
    """Task 3: 'quote every collateral across all Uniswap V3 fee tiers AND a no-key aggregator
    -- use the deeper.' Fits both independently and keeps whichever fit has the larger depth_cap
    (the largest size usable before marginal depth runs out) -- a direct comparison of how much
    this market could actually exit through, not just whichever source happened to answer.
    Returns (source, fit) where source is "onchain" or "aggregator", or None if neither fit.
    """
    onchain_fit = fit_slippage(onchain_points)
    aggregator_fit = fit_slippage(aggregator_points)
    if onchain_fit is None and aggregator_fit is None:
        return None
    if onchain_fit is None:
        return "aggregator", aggregator_fit
    if aggregator_fit is None:
        return "onchain", onchain_fit
    return ("onchain", onchain_fit) if onchain_fit[2] >= aggregator_fit[2] else ("aggregator", aggregator_fit)


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
    # q is a raw token amount and can exceed int64 for an 18-decimal, ~$1-priced token at the
    # multi-million-dollar quote sizes in QUOTE_SIZES_TOKEN -- found by hand when the universe
    # widened past WBTC/cbBTC (8 decimals, always in range): np.array() on a list of such Python
    # ints silently makes an object-dtype array instead of float64, and np.log then fails with
    # "int object has no attribute log". float(q) first avoids that; losing exactness here is
    # fine, this is already a float power-law fit, not integer-exact math.
    log_q = np.log([float(q) for q, _ in slips])
    log_slip = np.log([s for _, s in slips])
    b, log_a = np.polyfit(log_q, log_slip, 1)
    return float(np.exp(log_a)), float(b), depth_cap


def predict_slippage(a, b, q):
    return a * (q**b)


def stressed_slippage(a, b, q, kappa):
    """SPEC.md M6: under stress, depth shrinks: slip_s(q) = kappa * slip(q). Label it an
    assumption, not a measurement -- there is no live data source for depth under stress."""
    return kappa * predict_slippage(a, b, q)
