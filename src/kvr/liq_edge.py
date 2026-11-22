"""Task 4 (new module): "When do DeFi liquidators walk away, and who gets paid when they do?"

Per-market, per-liquidation-event reconstruction over config.LIQ_EDGE_LOOKBACK_DAYS: gas cost
(real tx receipts), DEX-vs-MM execution profitability, insolvency (badDebtAssets > 0), and
"left on the table" (unrecovered bad debt -- ground truth from the event itself, not a guess).

The expensive piece -- how long a position sat liquidatable before it was actually liquidated,
found by searching the ORACLE's own historical price against the position's breakeven price --
is capped per market (config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET events, searched via exponential-
backoff + binary refine so each one costs O(log(max_hours)) oracle reads, not a linear hourly
scan). Found by hand: the real 24-month universe has 886 Liquidate events; reconstructing all of
them this way would be well over an hour of RPC round trips for a number this project already
gets, bounded and labeled, from a sample -- the same precedent as T4's "replay the last 50".
"""

import statistics

import config
from . import dex, oracle
from . import chain as chain_mod
from . import morpho_math as mm


def breakeven_price(debt_raw, collateral_raw, lltv_raw):
    """The oracle price at which morpho_math.max_borrow(collateral, price, lltv) == debt_raw --
    where this position crosses from healthy to liquidatable. Inverts max_borrow's definition,
    (collateral*price // 1e36) * lltv // 1e18 == debt, algebraically (dropping the floor
    roundings): price = debt * 1e36 * 1e18 / (collateral * lltv). Float, not integer-exact --
    this is reconstruction/analytics, not a protocol replication (this project's own "integer-
    exact where the protocol is, float for analytics" rule). Hand-verified in tests/test_liq_edge.py.
    """
    if collateral_raw == 0 or lltv_raw == 0:
        return None
    return debt_raw * mm.ORACLE_PRICE_SCALE * mm.WAD / (collateral_raw * lltv_raw)


def position_debt_and_collateral(market_id, user, block):
    """Raw collateral and accrued-at-`block` debt for one position, via the same on-chain reads
    positions.py uses elsewhere (position() + market() at that exact block)."""
    mid_bytes = bytes.fromhex(market_id[2:])
    _supply_shares, borrow_shares, collateral = chain_mod.call_morpho("position", [mid_bytes, chain_mod.Web3.to_checksum_address(user)], block)
    _tsa, _tss, total_borrow_assets, total_borrow_shares, _lu, _fee = chain_mod.call_morpho("market", [mid_bytes], block)
    debt = mm.borrowed(borrow_shares, total_borrow_assets, total_borrow_shares)
    return debt, collateral


def _nearest_price(prices, ts):
    """prices: [(timestamp, price), ...]. Nearest-by-timestamp lookup, not interpolation -- good
    enough for valuing a single historical gas cost, which this is only used for."""
    if not prices:
        return None
    return min(prices, key=lambda p: abs(p[0] - ts))[1]


def gas_cost_usd(tx_hash, timestamp, weth_prices):
    """Real gasUsed * effectiveGasPrice from the transaction receipt, converted to USD via the
    nearest daily WETH price to the event's own timestamp (no extra RPC call for the block, since
    the Liquidate event already carries its own timestamp). None if the receipt can't be fetched
    or no price is available -- never a fabricated fallback cost."""
    try:
        receipt = chain_mod.w3().eth.get_transaction_receipt(tx_hash)
    except Exception:
        return None
    gas_eth = receipt["gasUsed"] * receipt["effectiveGasPrice"] / 1e18
    price = _nearest_price(weth_prices, timestamp)
    return gas_eth * price if price is not None else None


def event_economics(event, oracle_address, weth_prices, slip_fit):
    """Per-event profitability, insolvency, and left-on-the-table -- one oracle read (at the
    block just before the liquidation) plus one tx receipt, no hourly search. slip_fit: (a, b,
    depth_cap) from this market's own DEX-slippage fit (build_report.py's slippage_fits), or
    None if no DEX depth was found for this collateral at all -- DEX-side profitability is then
    left out (not guessed), while the MM-side (config.LIQ_EDGE_MM_SPREAD_BPS) is always computed.
    Returns None if the execution-time oracle price can't be read at all.
    """
    # The block the liquidation actually executed in, not the block before: found by hand that
    # the oracle price can (and often does) move within that single block -- a price-feed
    # update landing in the same block as the liquidation that it triggers is the common case,
    # not an edge case (confirmed on a real WBTC event: $56,758 at blockNumber-1 vs $56,015 at
    # blockNumber itself, the difference between "healthy" and "liquidatable" for that position).
    liq_block = event["blockNumber"]
    try:
        price = oracle.read_price(oracle_address, liq_block, max_retries=3)
    except Exception:
        return None

    seized = int(event["data"]["seizedAssets"])
    repaid = int(event["data"]["repaidAssets"])
    bad_debt = int(event["data"]["badDebtAssets"])
    seized_value_usd = seized * price / mm.ORACLE_PRICE_SCALE / 1e6
    repaid_usd = repaid / 1e6
    bad_debt_usd = bad_debt / 1e6
    gas_usd = gas_cost_usd(event["txHash"], event["timestamp"], weth_prices)

    mm_proceeds = seized_value_usd * (1 - config.LIQ_EDGE_MM_SPREAD_BPS / 10_000)
    profit_mm = (mm_proceeds - repaid_usd - gas_usd) if gas_usd is not None else None

    profit_dex = None
    if slip_fit is not None:
        a, b, _cap = slip_fit
        slip = min(dex.predict_slippage(a, b, seized), 1.0)
        dex_proceeds = seized_value_usd * (1 - slip)
        profit_dex = (dex_proceeds - repaid_usd - gas_usd) if gas_usd is not None else None

    return {
        "seized_value_usd": seized_value_usd,
        "repaid_usd": repaid_usd,
        "bad_debt_usd": bad_debt_usd,
        "is_insolvent": bad_debt > 0,
        "gas_usd": gas_usd,
        "profit_mm_usd": profit_mm,
        "profit_dex_usd": profit_dex,
    }


def _search_delay_hours(price_at_hours_back, max_hours, breakeven_px):
    """Pure search over price_at_hours_back(h) -> price or None. h=0 (the liquidation's own
    execution block) MUST already be breached -- that's why the liquidation happened -- so this
    checks that first and returns None if it isn't (or can't be read): found by hand on a real
    WBTC event where the oracle price moved from healthy to breached WITHIN the execution block
    itself (a price-feed update landing in the same block as the liquidation it triggers is the
    common case, not rare), so h=0 is not redundant with h=1 onward.

    From a confirmed breach at h=0, finds via exponential-backoff + binary-refine the largest h
    in [0, max_hours] for which price was STILL continuously <= breakeven_px -- i.e. how long
    the position had been liquidatable. O(log(max_hours)) calls, not a linear per-hour scan.
    Assumes that breach is one continuous stretch ending at h=0 -- a position that dipped below
    breakeven and recovered earlier wouldn't be found this way, and this reports only the delay
    since the LAST continuous breach, which is what "how long did this sit liquidatable" means
    here. Returns max_hours itself (a lower bound, not exact) if the breach holds continuously
    all the way back to the search window's edge -- a real limitation, not papered over.
    """
    p0 = price_at_hours_back(0)
    if p0 is None or p0 > breakeven_px:
        return None

    hour, last_breached, hi = 1, 0, None
    while hour <= max_hours:
        p = price_at_hours_back(hour)
        if p is None or p > breakeven_px:
            hi = hour
            break
        last_breached = hour
        hour *= 2
    if hi is None:
        return max_hours

    lo = last_breached
    while hi - lo > 1:
        mid = (lo + hi) // 2
        p = price_at_hours_back(mid)
        if p is not None and p <= breakeven_px:
            lo = mid
        else:
            hi = mid
    return lo


def delay_to_liquidation_hours(oracle_address, breakeven_px, liq_block, max_hours=None, blocks_per_hour=None):
    """liq_block: the block the liquidation actually executed in (event["blockNumber"], not
    blockNumber-1) -- see event_economics's docstring for why that distinction matters: the
    oracle price at h=0 must be the one Morpho's own liquidate() call actually saw.
    See _search_delay_hours. max_retries=3 here (not oracle.read_price's default 12): a
    missed probe in this bulk, exploratory search just reads as "no breach at this hour", which
    this project accepts as a labeled limitation (config.py) in exchange for ~900 fewer retries
    across a 230-event sample -- a single critical number elsewhere in this project still gets
    the full 12 retries (e.g. load_market's own oracle.read_price calls)."""
    max_hours = max_hours if max_hours is not None else config.LIQ_EDGE_MAX_DELAY_HOURS
    blocks_per_hour = blocks_per_hour if blocks_per_hour is not None else config.LIQ_EDGE_BLOCKS_PER_HOUR

    def price_at_hours_back(h):
        try:
            return oracle.read_price(oracle_address, liq_block - h * blocks_per_hour, max_retries=3)
        except Exception:
            return None

    return _search_delay_hours(price_at_hours_back, max_hours, breakeven_px)


def walk_away_breakpoint(economics_list, multipliers=(1, 1.5, 2, 3, 5, 8, 13, 21, 34)):
    """Smallest gas-cost multiplier at which at most half of this collateral's real historical
    events remain profitable (DEX side preferred, MM as fallback per event) -- a sensitivity
    swept over the ALREADY-observed real seized/repaid/gas numbers, not a new shock scenario.
    profit' = proceeds - repaid - gas*m = (profit + gas) - gas*m, so no need to recompute
    proceeds/repaid for each multiplier. Returns None if profitability can't be computed for any
    event (no gas data) or never drops to <=50% within the swept range.
    """
    base = [
        (e["profit_dex_usd"] if e["profit_dex_usd"] is not None else e["profit_mm_usd"], e["gas_usd"])
        for e in economics_list
        if e["gas_usd"] is not None and (e["profit_dex_usd"] is not None or e["profit_mm_usd"] is not None)
    ]
    if not base:
        return None
    for m in multipliers:
        share = sum(1 for profit, gas in base if (profit + gas - gas * m) > 0) / len(base)
        if share <= 0.5:
            return m
    return None


def summarize_market(events, economics_list, delay_samples):
    """economics_list and delay_samples are parallel to (a subset of, for delays) events --
    see build_liq_edge.py for how they're assembled. None entries (a failed oracle read for
    that specific event) are dropped, not treated as zero."""
    valid = [e for e in economics_list if e is not None]
    with_dex = [e for e in valid if e["profit_dex_usd"] is not None]
    with_mm = [e for e in valid if e["profit_mm_usd"] is not None]
    valid_delays = sorted(d for d in delay_samples if d is not None)
    return {
        "n_events": len(events),
        "n_insolvent": sum(1 for e in valid if e["is_insolvent"]),
        "profitable_share_dex": (sum(1 for e in with_dex if e["profit_dex_usd"] > 0) / len(with_dex)) if with_dex else None,
        "profitable_share_mm": (sum(1 for e in with_mm if e["profit_mm_usd"] > 0) / len(with_mm)) if with_mm else None,
        "total_left_on_table_usd": sum(e["bad_debt_usd"] for e in valid),
        "median_delay_hours": statistics.median(valid_delays) if valid_delays else None,
        "n_delay_sampled": len(delay_samples),
        "n_delay_found": len(valid_delays),
        "walk_away_gas_multiplier": walk_away_breakpoint(valid),
    }
