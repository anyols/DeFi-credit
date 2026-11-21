"""M10: spread decomposition, per market. Scope note (per SPEC.md section 7's own cut list,
applied given time constraints): the cross-section regression across the wider USDC-market
universe is cut, keeping only the per-market waterfall it explicitly allows dropping first.
"""

import time

import requests

from . import api, chain

AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
RAY = 10**27
AAVE_USDC_DEFILLAMA_POOL = "aa70268e-4b52-42bf-a116-608b370f9501"  # verified: apyBase matches the live on-chain rate


def aave_usdc_supply_rate(usdc_address, block):
    """Current on-chain Aave V3 USDC supply rate (fraction/year). Verified field layout against
    aave-v3-core's DataTypes.ReserveData -- currentLiquidityRate is ray-scaled (1e27)."""
    result = chain.call(AAVE_POOL, "getReserveData", [usdc_address], block)
    return result[2] / RAY


def aave_usdc_supply_rate_90d_avg(pool_id=AAVE_USDC_DEFILLAMA_POOL):
    try:
        r = requests.get(f"https://yields.llama.fi/chart/{pool_id}", timeout=30)
        r.raise_for_status()
        points = r.json()["data"][-90:]
        base = [p["apyBase"] for p in points if p.get("apyBase") is not None]
        return (sum(base) / len(base)) / 100 if base else None
    except Exception:
        return None


def _historical_series(market_id, field, chain_id=1, lookback_days=100):
    now = int(time.time())
    start = now - lookback_days * 86400
    q = f"""
    query($id: String!, $chainId: Int!, $start: Int, $end: Int) {{
      marketById(marketId: $id, chainId: $chainId) {{
        historicalState {{
          {field}(options: {{ startTimestamp: $start, endTimestamp: $end, interval: DAY }}) {{ x y }}
        }}
      }}
    }}
    """
    data = api.graphql(q, {"id": market_id, "chainId": chain_id, "start": start, "end": now})
    points = data["marketById"]["historicalState"][field]
    return [p["y"] for p in points if p["y"] is not None]


def market_quarterly_supply_apy(market_id, chain_id=1):
    """Most recent quarterlySupplyApy point already IS a trailing-90d average, per the Morpho API."""
    values = _historical_series(market_id, "quarterlySupplyApy", chain_id, lookback_days=100)
    return values[-1] if values else None


def utilization_time_at_cap(market_id, chain_id=1, threshold=0.99, lookback_days=90):
    """Fraction of daily observations over the lookback window with utilization >= threshold."""
    values = _historical_series(market_id, "utilization", chain_id, lookback_days)
    return sum(1 for v in values if v >= threshold) / len(values) if values else None


def spread_waterfall(supply_apy, risk_free_rate, el_bps_range, liquidity_frac_at_cap, liquidity_bps_per_unit_cap=50):
    """spread = EL + liquidity + residual. The liquidity_bps_per_unit_cap=50 scaling is an
    assumption (SPEC.md gives no formula, just says to price the liquidity proxy "via the
    cross-section" -- which is cut here, see module docstring), labeled as one.
    """
    spread_bps = (supply_apy - risk_free_rate) * 10_000
    el_mid = sum(el_bps_range) / 2 if el_bps_range else 0.0
    liquidity_bps = liquidity_frac_at_cap * liquidity_bps_per_unit_cap if liquidity_frac_at_cap is not None else 0.0
    residual_bps = spread_bps - el_mid - liquidity_bps
    return {"spread_bps": spread_bps, "el_bps": el_mid, "liquidity_bps": liquidity_bps, "residual_bps": residual_bps}
