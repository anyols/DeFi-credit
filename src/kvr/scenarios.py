"""M7: shock grid + historical frequencies.

Oracle-shock mapping (SPEC.md M7):
    MARKET:        m_o = m (the oracle sees the real move)
    FIXED:         m_o = 1 (the oracle never moves, regardless of m)
    EXCHANGE_RATE: m_o = 1 on a depeg-only shock, m_o = m on an underlying shock
"""

import numpy as np
import requests

import config

UNDERLYING_SHOCKS = tuple(round(-x / 100, 2) for x in range(10, 65, 5))  # -10% .. -60%
DEPEG_SHOCKS = tuple(round(-x / 100, 2) for x in range(2, 82, 2))  # -2% .. -80%, LST/LRT/yield-stable
DEPTH_STRESS_KAPPA = (1, 2, 4)
PATH_STEPS = (1, 6)  # 1 = gap, 6 = gradual

_DEFILLAMA_CHART = "https://coins.llama.fi/chart/{coin}"
_MAX_SPAN = 500  # DefiLlama's own per-request cap


def oracle_shock(market_shock, oracle_class, depeg_shock=False):
    if oracle_class == "FIXED":
        return 1.0
    if oracle_class == "EXCHANGE_RATE":
        return 1.0 if depeg_shock else market_shock
    return market_shock  # MARKET, and UNKNOWN treated conservatively as seeing the real move


def build_grid(oracle_class, is_depeg_asset=False):
    """Every (market_shock, oracle_class, kappa, path_steps) combination for one collateral
    asset. Each entry's oracle price ratio is derived later via market_shock_to_oracle_ratio --
    kept out of the grid itself so a single entry is reusable regardless of depeg framing.
    """
    shocks = DEPEG_SHOCKS if is_depeg_asset else UNDERLYING_SHOCKS
    grid = []
    for m in shocks:
        for kappa in DEPTH_STRESS_KAPPA:
            for k in PATH_STEPS:
                grid.append(
                    {
                        "market_shock": m,
                        "oracle_class": oracle_class,
                        "is_depeg_asset": is_depeg_asset,
                        "kappa": kappa,
                        "path_steps": k,
                    }
                )
    return grid


def market_shock_to_oracle_ratio(market_shock, oracle_class, is_depeg_asset=False):
    """Converts a market_shock fraction (e.g. -0.3) into the oracle's price ratio (e.g. 1.0 for
    a FIXED oracle, or 0.7 for a MARKET oracle seeing the same move)."""
    market_ratio = 1 + market_shock
    return oracle_shock(market_ratio, oracle_class, depeg_shock=is_depeg_asset)


def named_oracle_gap_scenario(market_shock=-0.3):
    """'Oracle gap': market moves, the oracle doesn't -- the general case of what a FIXED
    oracle or a depeg shock on an EXCHANGE_RATE oracle both look like mechanically."""
    return {"name": "oracle_gap", "market_shock": market_shock, "oracle_shock_ratio": 1.0, "kappa": 2, "path_steps": 1}


def fetch_daily_prices(coin_id, span=_MAX_SPAN, end=None):
    """coin_id like 'ethereum:0x...'. Returns a list of (timestamp, price), oldest first, or
    [] if DefiLlama has no data for this asset.

    Caution for EXCHANGE_RATE-oracle collateral (e.g. sNUSD): DefiLlama's series for these
    tokens tracks a smooth, monotonically-rising accounting NAV (186 days, std ~0.14%/day, max
    single-day move <1%, no sign of a secondary-market depeg) -- almost certainly the same kind
    of conversion-rate source the EXCHANGE_RATE oracle itself reads, not real secondary-market
    trading data. Using this series to estimate depeg exceedance frequency would be circular and
    would understate risk by construction (the NAV can't show a depeg the oracle can't see
    either). Callers should treat the depeg dimension for such assets as not estimable from this
    source, per SPEC.md M7, even though fetch_daily_prices returns non-empty data for it.
    """
    try:
        params = {"span": span, "period": "1d"}
        if end is not None:
            params["end"] = end
        r = requests.get(_DEFILLAMA_CHART.format(coin=coin_id), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()["coins"]
        if not data:
            return []
        points = next(iter(data.values()))["prices"]
        return [(p["timestamp"], p["price"]) for p in points]
    except Exception:
        return []


def fetch_price_history(coin_id, chunks=None):
    """Stitches config.PRICE_HISTORY_CHUNKS successive fetch_daily_prices windows together
    (DefiLlama caps a single request at _MAX_SPAN points) to reach back further than one window
    allows. Returns (timestamp, price) pairs, oldest first, deduplicated by timestamp.

    Why this exists, not just a bigger span: found while fixing the EL==0 bug that even a
    correctly 7d-declustered exceedance_frequency stayed ~0 on a single ~1.4-year window, because
    that window contained no BTC crash large enough to reach the shock grid's lossy buckets --
    a sample-length problem, not a horizon problem. See config.PRICE_HISTORY_CHUNKS.
    """
    chunks = chunks or config.PRICE_HISTORY_CHUNKS
    all_points = {}
    end = None
    for _ in range(chunks):
        points = fetch_daily_prices(coin_id, end=end)
        if not points:
            break
        for ts, price in points:
            all_points[ts] = price
        end = points[0][0] - 1  # next chunk ends just before this one's earliest point
    return sorted(all_points.items())


def exceedance_frequency(prices, horizon_days=1, declustering_days=None):
    """lambda(x): empirical annual exceedance frequency of a <= -x move over `horizon_days`,
    from declustered historical returns. Returns a callable lambda_fn(x) -> expected count/year,
    or None if there isn't enough history to say anything (SPEC.md: 'not estimable, judgment only').

    declustering_days defaults to horizon_days, not 1: rolling horizon_days-day returns overlap
    by (horizon_days - 1) days between consecutive observations, so a single real crash shows up
    as horizon_days nearly-identical "events" unless declustering spaces kept events at least
    horizon_days apart. Found by hand while diagnosing an EL==0 bug: the 1-day-horizon version
    of this already (correctly) uses declustering_days=1, which is why that specific bug wasn't
    a declustering problem -- but get this wrong for horizon_days=7 and EL would be too HIGH
    (double-counting one crash ~7x), the opposite-direction version of the same mistake.
    """
    declustering_days = declustering_days if declustering_days is not None else horizon_days
    if len(prices) < horizon_days + 10:
        return None
    values = np.array([p for _, p in prices])
    returns = values[horizon_days:] / values[:-horizon_days] - 1

    declustered = []
    last_kept_idx = -declustering_days - 1
    for i in np.argsort(returns):  # worst first
        if i - last_kept_idx >= declustering_days:
            declustered.append(returns[i])
            last_kept_idx = i
    declustered = np.array(declustered)
    years_covered = (prices[-1][0] - prices[0][0]) / (365.25 * 86400)
    if years_covered <= 0:
        return None

    def lambda_fn(x):
        count = np.sum(declustered <= -abs(x))
        return count / years_covered

    return lambda_fn
