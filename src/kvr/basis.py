

import requests

import config
from . import backtest, chain, irm, stream_case
from . import morpho_math as mm

BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"


def fetch_funding_history(start_ts, end_ts, symbol=None):
    """8-hourly ETH perp funding rate history from Binance's public API -- verified by hand to
    need no key. Paginated at 1000/request (Binance's own cap). Returns [(unix_ts, rate), ...],
    rate as the raw per-8h fraction Binance reports (e.g. 0.0000291 = 0.00291%), Binance's own
    sign convention: positive = longs pay shorts, so a SHORT perp position (this trade's own
    position) receives funding when the rate is positive.
    """
    symbol = symbol or config.BASIS_PERP_SYMBOL
    out = []
    cursor_ms = start_ts * 1000
    end_ms = end_ts * 1000
    while cursor_ms < end_ms:
        r = requests.get(BINANCE_FUNDING_URL, params={"symbol": symbol, "startTime": cursor_ms, "endTime": end_ms, "limit": 1000}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for item in batch:
            out.append((item["fundingTime"] // 1000, float(item["fundingRate"])))
        cursor_ms = batch[-1]["fundingTime"] + 1
        if len(batch) < 1000:
            break
    return out


def daily_funding_rate(funding_history, day):
    """Sum of the day's (up to 3) 8-hourly funding payments -- the actual daily fraction a short
    perp position receives (or pays, if negative) that day. None if no funding event fell on
    this day (a real gap, not assumed zero)."""
    total, n = 0.0, 0
    for ts, rate in funding_history:
        if backtest._day(ts) == day:
            total += rate
            n += 1
    return total if n > 0 else None


def fetch_wsteth_staking_yield_samples(window_start_ts, window_end_ts, sample_days=None):
    """wstETH's own on-chain conversion rate (stEthPerToken), sampled every sample_days across
    the window -- see config.BASIS_STAKING_YIELD_SAMPLE_DAYS's own docstring for why this is
    coarser than daily. Returns [(unix_ts, raw_rate), ...], oldest first, including the exact
    window end even if it doesn't land on a sample boundary."""
    sample_days = sample_days if sample_days is not None else config.BASIS_STAKING_YIELD_SAMPLE_DAYS
    samples = []
    ts = window_start_ts
    while ts < window_end_ts:
        block = stream_case.find_block_by_timestamp(ts, w3=chain.w3())
        (rate,) = chain.call(WSTETH, "stEthPerToken", [], block)
        samples.append((ts, rate))
        ts += sample_days * 86400
    block = stream_case.find_block_by_timestamp(window_end_ts, w3=chain.w3())
    (rate,) = chain.call(WSTETH, "stEthPerToken", [], block)
    samples.append((window_end_ts, rate))
    return samples


def annualized_staking_yield_at(samples, day):
    """Piecewise-constant annualized staking yield: for the sampled bracket [t0,t1] containing
    `day`, yield = (rate1/rate0 - 1) * (365*86400/(t1-t0)). None if `day` falls outside the
    sampled range."""
    for i in range(len(samples) - 1):
        t0, r0 = samples[i]
        t1, r1 = samples[i + 1]
        if t0 <= day <= t1 and t1 > t0:
            return (r1 / r0 - 1) * (365 * 86400 / (t1 - t0))
    return None


def daily_borrow_rate_per_second(utilization_float, rate_at_target_raw):
    """The raw IRM borrow rate a BORROWER pays (not backtest.daily_rate_per_second's SUPPLY
    rate, which is already scaled by utilization*(1-fee) for the lender's side)."""
    if utilization_float is None or rate_at_target_raw is None:
        return None
    u_wad = int(min(max(utilization_float, 0.0), 1.0) * mm.WAD)
    borrow_rate_per_second, _end_rt = irm.borrow_rate(u_wad, int(rate_at_target_raw), 0)
    return borrow_rate_per_second / mm.WAD


def daily_carry(day, funding_history, staking_samples, wsteth_morpho_history, perp_fee_bps=None, target_ltv=None):
    """Annualized carry for BASIS and LEVERED BASIS on this specific day. None for either figure
    individually if that day's own inputs are missing (a real data gap, never silently zeroed).
    """
    perp_fee_bps = perp_fee_bps if perp_fee_bps is not None else config.BASIS_PERP_FEE_BPS_ROUNDTRIP
    target_ltv = target_ltv if target_ltv is not None else config.BASIS_TARGET_LTV

    funding_daily = daily_funding_rate(funding_history, day)
    staking_annual = annualized_staking_yield_at(staking_samples, day)
    funding_annual = funding_daily * 365 if funding_daily is not None else None
    fee_annual_drag = (perp_fee_bps / 10_000) * (365 / config.BASIS_LOOKBACK_DAYS)

    basis_carry = None
    if staking_annual is not None and funding_annual is not None:
        basis_carry = staking_annual + funding_annual - fee_annual_drag

    morpho_rec = wsteth_morpho_history.get(day)
    morpho_borrow_apr = None
    if morpho_rec is not None:
        rate_ps = daily_borrow_rate_per_second(morpho_rec.get("utilization"), morpho_rec.get("rateAtTarget"))
        if rate_ps is not None:
            morpho_borrow_apr = rate_ps * irm.SECONDS_PER_YEAR

    leverage = 1 / (1 - target_ltv)
    levered_carry = None
    if basis_carry is not None and morpho_borrow_apr is not None:
        levered_carry = leverage * (staking_annual + funding_annual) - (leverage - 1) * morpho_borrow_apr - fee_annual_drag

    return {
        "day": day,
        "funding_annual": funding_annual,
        "staking_annual": staking_annual,
        "morpho_borrow_apr": morpho_borrow_apr,
        "basis_carry": basis_carry,
        "levered_carry": levered_carry,
    }


def depeg_to_liquidation(target_ltv, lltv):
    """How far wstETH's oracle-implied value can drop (relative to the borrowed USDC) before
    the levered position hits Morpho's own LLTV, via morpho_math.max_borrow's own definition:
    at target_ltv, debt == target_ltv * collateral_value. Liquidation triggers once
    debt > max_borrow(collateral, price, lltv) == collateral_value * lltv, i.e. once the
    collateral value has fallen by a fraction d where target_ltv > (1-d)*lltv -- solving gives
    d > 1 - target_ltv/lltv. Hand-verified with representative integers in tests/test_basis.py.
    """
    return 1 - target_ltv / lltv


def perp_margin_buffer_needed(leverage, shock_pct):
    """USD loss on the short perp leg, per $1 of OWN equity (not notional) in the levered basis
    trade, for a +shock_pct move in ETH -- the margin a trader needs beyond the trade's own
    equity to avoid the perp's OWN liquidation (separate from the Morpho LLTV check above). A
    rising ETH price moves against a short perp 1:1 in notional, and notional = leverage * equity.
    """
    return leverage * shock_pct
