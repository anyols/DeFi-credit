"""M10: spread decomposition, per market and across the wider USDC-market cross-section."""

import time

import numpy as np
import requests

import config
from . import api, chain, el, irm, oracle

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
RAY = 10**27
AAVE_USDC_DEFILLAMA_POOL = "aa70268e-4b52-42bf-a116-608b370f9501"  # verified: apyBase matches the live on-chain rate
USDC_ETHEREUM = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


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
    # Sorted ascending (oldest first) explicitly -- found by hand that the API actually returns
    # these newest-first, which silently broke consecutive_days_at_utilization_cap's assumption
    # that reversed(values) would walk from most-recent backward (it instead walked oldest-first,
    # since the input was already newest-first) until this sort was added.
    return sorted(((p["x"], p["y"]) for p in points if p["y"] is not None), key=lambda t: t[0])


def _onchain_borrow_rate_and_utilization(market_id, block):
    """(borrow_rate_per_second, utilization_wad, fee_wad) or None if idle/no supply."""
    mid_bytes = bytes.fromhex(market_id[2:])
    total_supply_assets, _tss, total_borrow_assets, _tbs, _lu, fee = chain.call_morpho("market", [mid_bytes], block)
    if total_supply_assets == 0:
        return None
    params = chain.call_morpho("idToMarketParams", [mid_bytes], block)
    irm_addr = params[3]
    if irm_addr.lower() == ZERO_ADDRESS:
        return None
    (rate_at_target,) = chain.call(irm_addr, "rateAtTarget", [mid_bytes], block)
    u = irm.utilization(total_borrow_assets, total_supply_assets)
    borrow_rate_per_second, _end_rt = irm.borrow_rate(u, rate_at_target, 0)
    return borrow_rate_per_second, u, fee


def onchain_supply_apy(market_id, block):
    """Current, point-in-time supply APY computed directly from on-chain state -- borrow_apy *
    utilization * (1-fee), via the same irm.borrow_rate path T3 already proved matches
    borrowRateView bit-for-bit. Not a trailing average: Task 2's own instruction is to stop
    trusting the Morpho API's APY fields at all (T5 found monthlyApy/monthlySupplyApy simply
    wrong for a real market), so this replaces market_quarterly_supply_apy rather than
    supplementing it. Returns 0.0 for a market with no IRM (idle markets have no borrowing, so
    no yield) or no supply at all. For a zombie market, prefer onchain_supply_apr instead (see
    irm.apy_from_per_second_rate's docstring for why APY is the wrong figure there).
    """
    result = _onchain_borrow_rate_and_utilization(market_id, block)
    if result is None:
        return 0.0
    borrow_rate_per_second, u, fee = result
    borrow_apy = irm.apy_from_per_second_rate(borrow_rate_per_second)
    return borrow_apy * (u / 10**18) * (1 - fee / 10**18)


def onchain_supply_apr(market_id, block):
    """Simple (non-compounded) supply APR -- see irm.apr_from_per_second_rate. Use this, not
    onchain_supply_apy, for a zombie market (Task 2 review point 3): APY's continuous
    compounding of an already-extreme pinned rate produces a multi-million-percent figure that
    isn't a real, earnable yield; APR just scales the per-second rate linearly and stays sane.
    """
    result = _onchain_borrow_rate_and_utilization(market_id, block)
    if result is None:
        return 0.0
    borrow_rate_per_second, u, fee = result
    borrow_apr = irm.apr_from_per_second_rate(borrow_rate_per_second)
    return borrow_apr * (u / 10**18) * (1 - fee / 10**18)


def utilization_time_at_cap(market_id, chain_id=1, threshold=0.99, lookback_days=90):
    """Fraction of daily observations over the lookback window with utilization >= threshold."""
    points = _historical_series(market_id, "utilization", chain_id, lookback_days)
    return sum(1 for _ts, v in points if v >= threshold) / len(points) if points else None


def consecutive_days_at_utilization_cap(market_id, chain_id=1, threshold=None, max_lookback_days=400):
    """(days, is_lower_bound). days: walking backward from the most recent observation, how
    long utilization has stayed >= threshold without a break; 0 if the most recent observation
    is already below threshold. is_lower_bound=True means the entire max_lookback_days window
    was at-cap -- the real streak could be longer, this just couldn't see further back. Found by
    hand: a 180-day window reported exactly 181 for 4 of 5 pinned markets (a suspiciously
    round, identical number across different markets), and a 400-day window showed their real
    streak is 246 days -- so 181 was itself a lower bound, not a fact, and a caller that doesn't
    check is_lower_bound would under-report days_stuck as if it were exact.
    """
    threshold = threshold if threshold is not None else config.ZOMBIE_UTIL_THRESHOLD
    points = _historical_series(market_id, "utilization", chain_id, max_lookback_days)
    days = 0
    for _ts, v in reversed(points):  # points is ascending (oldest first) -- reversed walks newest first
        if v < threshold:
            return days, False
        days += 1
    return days, (days == len(points) and days > 0)


def classify_zombie(market_id, before_timestamp, chain_id=1):
    """Task 2 review point 3: utilization >= config.ZOMBIE_UTIL_THRESHOLD for
    >= config.ZOMBIE_MIN_DAYS_STUCK consecutive days, AND no Repay event in the last
    config.ZOMBIE_MIN_DAYS_SINCE_REPAY days. Returns a dict with the fields the memo needs
    (days_stuck, last_repay_date, is_zombie) regardless of the verdict, not just a bool.
    """
    days_stuck, days_stuck_is_lower_bound = consecutive_days_at_utilization_cap(market_id, chain_id)
    last_repay = api.get_last_transaction(market_id, "Repay", before_timestamp=before_timestamp)
    if last_repay is None:
        days_since_repay = None  # never repaid at all -- at least as stuck as any finite count
        is_stale = True
    else:
        days_since_repay = (before_timestamp - last_repay["timestamp"]) / 86400
        is_stale = days_since_repay >= config.ZOMBIE_MIN_DAYS_SINCE_REPAY
    is_zombie = days_stuck >= config.ZOMBIE_MIN_DAYS_STUCK and is_stale
    return {
        "is_zombie": is_zombie,
        "days_stuck": days_stuck,
        "days_stuck_is_lower_bound": days_stuck_is_lower_bound,
        "last_repay_timestamp": last_repay["timestamp"] if last_repay else None,
        "days_since_repay": days_since_repay,
    }


def spread_waterfall(supply_apy, risk_free_rate, el_bps_range, liquidity_frac_at_cap, liquidity_bps_per_unit_cap=50):
    """spread = EL + liquidity + residual. The liquidity_bps_per_unit_cap=50 scaling is an
    assumption (SPEC.md gives no formula for pricing the liquidity proxy) -- cross_section_ols
    below is what "via the cross-section" means; this constant is a placeholder until that
    regression's own liquidity coefficient is substituted in by the caller.
    """
    spread_bps = (supply_apy - risk_free_rate) * 10_000
    el_mid = sum(el_bps_range) / 2 if el_bps_range else 0.0
    liquidity_bps = liquidity_frac_at_cap * liquidity_bps_per_unit_cap if liquidity_frac_at_cap is not None else 0.0
    residual_bps = spread_bps - el_mid - liquidity_bps
    return {"spread_bps": spread_bps, "el_bps": el_mid, "liquidity_bps": liquidity_bps, "residual_bps": residual_bps}


# --- Cross-section (SPEC.md M10): all Ethereum USDC markets with supply > $1M -------------

def usdc_market_universe(min_supply_usd=1_000_000, chain_id=1, first=300):
    q = """
    query($loan: [String!], $chainIds: [Int!], $minSupply: Float, $first: Int) {
      markets(first: $first, where: { loanAssetAddress_in: $loan, chainId_in: $chainIds, supplyAssetsUsd_gte: $minSupply }) {
        items {
          marketId lltv
          loanAsset { symbol decimals address }
          collateralAsset { symbol address decimals }
          oracle { address }
          irmAddress
          state { supplyAssetsUsd borrowAssetsUsd utilization quarterlySupplyApy collateralAssetsUsd fee }
        }
      }
    }
    """
    data = api.graphql(q, {"loan": [USDC_ETHEREUM], "chainIds": [chain_id], "minSupply": min_supply_usd, "first": first})
    return data["markets"]["items"]


_COLLATERAL_WITH_TAIL_HISTORY = ("BTC", "ETH", "STETH", "RETH")  # see classify_collateral


def classify_collateral(symbol):
    """A simple, labeled heuristic for the regression's dummy variable -- not a rigorous
    taxonomy. BTC/ETH-like collateral gets a lambda(x) in el_proxy_bps (BTC's, as a proxy, even
    for ETH -- both liquid majors; genuinely imprecise but better than nothing for a descriptive
    regression). Everything else (PT fixed-maturity notes, RWA wrappers, obscure yield-bearing
    stables) gets EL marked not estimable, per SPEC.md's own rule against inventing a number --
    these are mostly illiquid, little-traded tokens with no usable price history at all.
    """
    if symbol is None:
        return "idle"
    s = symbol.upper()
    if s.startswith("PT-"):
        return "PT (fixed maturity)"
    if "BTC" in s:
        return "BTC"
    if any(k in s for k in ("ETH", "STETH", "RETH")):
        return "ETH/LST"
    if any(k in s for k in ("USD", "USR", "USN", "EUR")):
        return "stable/yield-bearing"
    return "RWA/other"


def has_tail_history_proxy(collateral_class):
    return collateral_class in ("BTC", "ETH/LST")


def batch_classify_oracles(oracle_addresses, block, batch_size=300):
    """Classifies many oracles in one pass via multicall instead of one eth_call per field per
    oracle (6 fields x 66 markets would otherwise be 396 separate round trips)."""
    fields = oracle._FEED_FIELDS + oracle._VAULT_FIELDS
    addresses = list(oracle_addresses)
    calls = [(addr, fn, []) for addr in addresses for fn in fields]
    results = chain.multicall(calls, block, batch_size=batch_size)

    out = {}
    idx = 0
    for addr in addresses:
        values = {}
        for fn in fields:
            r = results[idx]
            values[fn] = r[0] if r else None
            idx += 1
        if all(v is None for v in values.values()):
            out[addr] = "UNKNOWN"
        elif any(oracle._is_set(values[k]) for k in oracle._FEED_FIELDS):
            out[addr] = "MARKET"
        elif any(oracle._is_set(values[k]) for k in oracle._VAULT_FIELDS):
            out[addr] = "EXCHANGE_RATE"
        else:
            out[addr] = "FIXED"
    return out


def el_proxy_bps(supply_usd, borrow_usd, collateral_usd, lambda_fn, shocks):
    """Aggregate-level EL proxy: treats the whole market's collateral/debt as one position (no
    per-borrower distribution, no LIF/slippage -- just max(0, borrow_usd - shocked collateral
    value) / supply_usd, matching stress.py's economic_loss definition but computed in aggregate
    instead of per-position). A real simplification, for cross-sectional comparison only --
    the universe's own markets get the full per-position M8 treatment elsewhere.
    """
    if lambda_fn is None or not collateral_usd or not borrow_usd or not supply_usd:
        return None
    shocks_sorted = sorted(shocks)
    freqs = el.marginal_frequencies(shocks_sorted, lambda_fn)
    el_frac = 0.0
    for s in shocks_sorted:
        shocked_collateral = collateral_usd * (1 + s)
        loss = max(0.0, borrow_usd - shocked_collateral)
        el_frac += freqs[s] * (loss / supply_usd)
    return el_frac * 10_000


def aave_morpho_funded_carry(ltv, r_morpho_supply, r_aave_borrow, collateral_yield=0.0):
    """SPEC.md M10's 'Why Aave->Morpho isn't arbitrage' box: the carry from a trader who already
    holds the collateral, borrows USDC against it on Aave, and deposits that USDC into the
    Morpho market -- a funded trade exposed to Aave's own liquidation risk on the collateral and
    Morpho's credit risk on the supply side (the same risk this whole project prices).
    """
    return collateral_yield + ltv * (r_morpho_supply - r_aave_borrow)


def ols_regress(y, feature_matrix, feature_names):
    """Plain OLS via lstsq (adds its own intercept column) -- no new dependency for a
    descriptive regression. Returns coefficients (incl. intercept), R^2, and n.
    """
    y = np.asarray(y, dtype=float)
    X = np.column_stack([np.ones(len(y)), np.asarray(feature_matrix, dtype=float)])
    coeffs, _residuals, _rank, _sv = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ coeffs
    ss_res = np.sum((y - fitted) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    names = ["intercept"] + list(feature_names)
    return {"coefficients": dict(zip(names, coeffs.tolist())), "r_squared": float(r_squared), "n": len(y)}
