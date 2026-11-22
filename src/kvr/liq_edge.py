"""Task 4 (new module): "When do DeFi liquidators walk away, and who gets paid when they do?"

Per-market, per-liquidation-event reconstruction over config.LIQ_EDGE_LOOKBACK_DAYS: gas cost
(real tx receipts, priced via a block-level Chainlink ETH/USD read -- review fix 1), per-
collateral-class exit economics (review fix 2: perp hedge for BTC/ETH, issuer redemption for
redeemable yield-bearing collateral, hold-to-maturity for PT notes), insolvency (badDebtAssets
> 0), and "left on the table" (unrecovered bad debt -- ground truth from the event itself).

The expensive piece -- how long a position sat liquidatable before it was actually liquidated,
found by searching the ORACLE's own historical price against the position's breakeven price --
is capped per market (config.LIQ_EDGE_DELAY_SAMPLE_PER_MARKET events, searched via exponential-
backoff + binary refine so each one costs O(log(max_hours)) oracle reads, not a linear hourly
scan). Found by hand: the real 24-month universe has 886 Liquidate events; reconstructing all of
them this way would be well over an hour of RPC round trips for a number this project already
gets, bounded and labeled, from a sample -- the same sampling precedent as T4's "replay the last 50".
"""

import datetime as dt
import re
import statistics

import config
from . import dex, oracle
from . import chain as chain_mod
from . import morpho_math as mm

ETH_USD_CHAINLINK_FEED = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"  # verified by hand, see chain.py


def breakeven_price(debt_raw, collateral_raw, lltv_raw):
    """The oracle price at which morpho_math.max_borrow(collateral, price, lltv) == debt_raw --
    where this position crosses from healthy to liquidatable. Inverts max_borrow's definition,
    (collateral*price // 1e36) * lltv // 1e18 == debt, algebraically (dropping the floor
    roundings): price = debt * 1e36 * 1e18 / (collateral * lltv). Float, not integer-exact --
    this is reconstruction/analytics, not a protocol replication (this project's own "integer-
    exact where the protocol is, float for analytics" rule). Hand-verified in tests/test_t8_t9_liq_edge.py.
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


def eth_usd_price_at_block(block, max_retries=5):
    """Chainlink's own ETH/USD feed, read directly (not via a Morpho market's oracle) and at the
    exact block -- review fix 1: replaces the old DefiLlama-daily-price gas conversion, which
    could be off by whatever ETH moved intraday. Retries on a non-positive answer, same
    discipline as oracle.read_price (a 0 or negative price is never accepted as real)."""
    for _ in range(max_retries):
        _round_id, answer, _started, _updated, _answered_in_round = chain_mod.call(ETH_USD_CHAINLINK_FEED, "latestRoundData", [], block)
        if answer > 0:
            return answer / 10**8  # feed is 8-decimal, verified by hand (chain.py)
    raise RuntimeError(f"ETH/USD Chainlink feed returned a non-positive answer on all {max_retries} attempts at block {block}")


def gas_cost_usd(tx_hash, block):
    """Real gasUsed * effectiveGasPrice from the transaction receipt, converted to USD via the
    block-level Chainlink ETH/USD price (review fix 1) -- not a daily DefiLlama close. None if
    the receipt or the feed read fails -- never a fabricated fallback cost."""
    try:
        receipt = chain_mod.w3().eth.get_transaction_receipt(tx_hash)
        eth_usd = eth_usd_price_at_block(block)
    except Exception:
        return None
    gas_eth = receipt["gasUsed"] * receipt["effectiveGasPrice"] / 1e18
    return gas_eth * eth_usd


_PT_MATURITY_RE = re.compile(r"(\d{1,2})([A-Za-z]{3})(\d{4})$")


def parse_pt_maturity(symbol):
    """"PT-reUSD-10DEC2026" -> date(2026, 12, 10). None if the symbol doesn't match this
    Pendle-style naming convention -- never a guessed maturity."""
    m = _PT_MATURITY_RE.search(symbol)
    if not m:
        return None
    day, mon, year = m.groups()
    try:
        return dt.datetime.strptime(f"{day}{mon.title()}{year}", "%d%b%Y").replace(tzinfo=dt.timezone.utc).date()
    except ValueError:
        return None


def event_economics(event, oracle_address, symbol, collateral_class, slip_fit, cost_of_capital_annual):
    """Per-event profitability, insolvency, and left-on-the-table -- one oracle read (at the
    liquidation's own execution block, not the block before -- see the note below) plus one tx
    receipt, no hourly search.

    Review fix 1 decomposition: oracle_implied_bonus_usd (the LIF spread, valued at the market's
    own oracle price -- used as the market-price proxy for BTC/ETH per the review's own offered
    alternative to a separate Chainlink round, so oracle_market_basis_usd is 0 by construction,
    not independently verified against a second feed) and gas_usd (block-level, see gas_cost_usd).

    Review fix 2: the exit model (and therefore which cost is subtracted from the bonus) depends
    on collateral_class/symbol -- see config.py's own LIQ_EDGE_HEDGE_COST_BPS /
    LIQ_EDGE_REDEMPTION_COOLDOWN_DAYS / LIQ_EDGE_REDEMPTION_HAIRCUT_BPS docstrings for what each
    model assumes. profit_dex_usd (the actual, Task-3-verified on-chain/aggregator DEX route) is
    kept alongside the exit-model profit for BTC/ETH-family collateral only, as a second,
    independently observed execution channel -- "MM" is gone entirely for everything else, per
    the review ("remove MM P&L for collateral with no liquid perp hedge").
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
    gas_usd = gas_cost_usd(event["txHash"], liq_block)

    oracle_implied_bonus_usd = seized_value_usd - repaid_usd
    oracle_market_basis_usd = 0.0  # oracle used as the market-price proxy -- see docstring

    profit_dex = None
    if slip_fit is not None:
        a, b, _cap = slip_fit
        slip = min(dex.predict_slippage(a, b, seized), 1.0)
        dex_proceeds = seized_value_usd * (1 - slip)
        profit_dex = (dex_proceeds - repaid_usd - gas_usd) if gas_usd is not None else None

    exit_model, exit_cost_usd, exit_note = _exit_cost(
        symbol, collateral_class, seized_value_usd, event["timestamp"], cost_of_capital_annual
    )
    profit_usd = None
    if gas_usd is not None and exit_cost_usd is not None:
        profit_usd = oracle_implied_bonus_usd - oracle_market_basis_usd - exit_cost_usd - gas_usd

    return {
        "seized_value_usd": seized_value_usd,
        "repaid_usd": repaid_usd,
        "bad_debt_usd": bad_debt_usd,
        "is_insolvent": bad_debt > 0,
        "is_dust": repaid_usd < config.LIQ_EDGE_DUST_THRESHOLD_USD,
        "gas_usd": gas_usd,
        "oracle_implied_bonus_usd": oracle_implied_bonus_usd,
        "oracle_market_basis_usd": oracle_market_basis_usd,
        "exit_model": exit_model,
        "exit_cost_usd": exit_cost_usd,
        "exit_note": exit_note,
        "profit_usd": profit_usd,
        "profit_dex_usd": profit_dex,
    }


def _exit_cost(symbol, collateral_class, seized_value_usd, event_timestamp, cost_of_capital_annual):
    """Review fix 2's per-collateral-class exit model. Returns (exit_model_name, cost_usd_or_None,
    note_or_None). cost=None means "not estimable" (never guessed), not zero.
    """
    if collateral_class in ("BTC", "ETH/LST"):
        return "perp_hedge", seized_value_usd * config.LIQ_EDGE_HEDGE_COST_BPS / 10_000, None

    if collateral_class == "PT (fixed maturity)":
        maturity = parse_pt_maturity(symbol)
        if maturity is None:
            return "hold_to_maturity", None, "could not parse maturity date from symbol"
        event_date = dt.datetime.fromtimestamp(event_timestamp, tz=dt.timezone.utc).date()
        days_to_maturity = max(0, (maturity - event_date).days)
        carry_cost = seized_value_usd * cost_of_capital_annual * (days_to_maturity / 365)
        return "hold_to_maturity", carry_cost, f"{days_to_maturity}d carry to maturity {maturity.isoformat()}"

    if symbol in config.LIQ_EDGE_REDEMPTION_COOLDOWN_DAYS:
        cooldown_days = config.LIQ_EDGE_REDEMPTION_COOLDOWN_DAYS[symbol]
        haircut_bps = config.LIQ_EDGE_REDEMPTION_HAIRCUT_BPS[symbol]
        if cooldown_days is None or haircut_bps is None:
            return "issuer_redemption", None, "redemption cooldown/fee not verified -- see config.py"
        cost = seized_value_usd * (haircut_bps / 10_000 + cost_of_capital_annual * (cooldown_days / 365))
        return "issuer_redemption", cost, None

    return "none", None, "no verified exit model for this collateral"


def profitability_stats(economics_list):
    """Review fix 1: profitable share by COUNT and by USD VOLUME (volume-weighted by repaid_usd),
    over non-dust events with a computed profit only -- dust is reported separately (count +
    volume), never silently folded into the headline share."""
    valid = [e for e in economics_list if e is not None]
    dust = [e for e in valid if e["is_dust"]]
    real = [e for e in valid if not e["is_dust"] and e["profit_usd"] is not None]
    n_real = len(real)
    volume = sum(e["repaid_usd"] for e in real)
    profitable_volume = sum(e["repaid_usd"] for e in real if e["profit_usd"] > 0)
    return {
        "n_dust": len(dust),
        "dust_volume_usd": sum(e["repaid_usd"] for e in dust),
        "n_priced": n_real,
        "profitable_share_count": (sum(1 for e in real if e["profit_usd"] > 0) / n_real) if n_real else None,
        "profitable_share_volume": (profitable_volume / volume) if volume > 0 else None,
        "total_volume_usd": volume,
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
    this project accepts as a labeled limitation (config.py) in exchange for far fewer retries
    across a 230+-event sample -- a single critical number elsewhere in this project still gets
    the full 12 retries (e.g. load_market's own oracle.read_price calls)."""
    max_hours = max_hours if max_hours is not None else config.LIQ_EDGE_MAX_DELAY_HOURS
    blocks_per_hour = blocks_per_hour if blocks_per_hour is not None else config.LIQ_EDGE_BLOCKS_PER_HOUR

    def price_at_hours_back(h):
        try:
            return oracle.read_price(oracle_address, liq_block - h * blocks_per_hour, max_retries=3)
        except Exception:
            return None

    return _search_delay_hours(price_at_hours_back, max_hours, breakeven_px)


def delay_to_liquidation_hours_extended(oracle_address, breakeven_px, liq_block, blocks_per_hour=None):
    """Review fix 4: a cheap search first (config.LIQ_EDGE_MAX_DELAY_HOURS); if that hits its
    own bound (the breach predates it -- the common shape for a market that drifted into
    insolvency via accrued interest rather than a price crash, e.g. AZND, RLP, USR, wstUSR),
    retries once at config.LIQ_EDGE_MAX_DELAY_HOURS_EXTENDED (the full lookback window) instead
    of reporting a number truncated by a search budget that was never meant to be a hard fact.
    Still returns the extended bound itself (not an exact value) if even that doesn't find the
    breach -- still a lower bound, just a much less likely one to bind in practice.
    """
    cheap = delay_to_liquidation_hours(oracle_address, breakeven_px, liq_block, max_hours=config.LIQ_EDGE_MAX_DELAY_HOURS, blocks_per_hour=blocks_per_hour)
    if cheap != config.LIQ_EDGE_MAX_DELAY_HOURS:
        return cheap
    return delay_to_liquidation_hours(
        oracle_address, breakeven_px, liq_block, max_hours=config.LIQ_EDGE_MAX_DELAY_HOURS_EXTENDED, blocks_per_hour=blocks_per_hour
    )


def decompose_hf_decline(collateral, lltv_raw, debt_breach, debt_exec, price_breach, price_exec):
    """Review fix 4: approximate attribution of the HF decline from breach (HF ~= 1.0 there, by
    definition -- that's what "breach" means) to execution, into price-driven vs interest-
    driven, by holding one side fixed at a time. These two figures don't exactly sum to the
    total decline (there's a cross term) -- a standard, approximate attribution, labeled as
    such, not an exact decomposition. collateral is assumed unchanged over the window (no
    SupplyCollateral/WithdrawCollateral during the delay) -- a labeled simplification.
    """
    hf_if_only_interest_grew = mm.max_borrow(collateral, price_breach, lltv_raw) / debt_exec if debt_exec else None
    hf_if_only_price_moved = mm.max_borrow(collateral, price_exec, lltv_raw) / debt_breach if debt_breach else None
    interest_driven_decline = (1.0 - hf_if_only_interest_grew) if hf_if_only_interest_grew is not None else None
    price_driven_decline = (1.0 - hf_if_only_price_moved) if hf_if_only_price_moved is not None else None
    label = None
    if interest_driven_decline is not None and price_driven_decline is not None:
        label = "interest-driven" if interest_driven_decline > price_driven_decline else "price-driven"
    return {
        "interest_driven_decline": interest_driven_decline,
        "price_driven_decline": price_driven_decline,
        "label": label,
        "debt_growth_from_interest_usd": (debt_exec - debt_breach) / 1e6,
        "price_change_pct": (price_exec / price_breach - 1) if price_breach else None,
    }


def walk_away_breakpoint(economics_list, multipliers=(1, 1.5, 2, 3, 5, 8, 13, 21, 34)):
    """Observed sensitivity (review fix 5: labeled as such, distinct from the stress engine's
    modeled breakpoint_shock): smallest gas-cost multiplier at which at most half of this
    collateral's real historical events remain profitable, swept over the ALREADY-observed real
    numbers (profit_usd, gas_usd), not a new shock scenario. profit' = proceeds - repaid - gas*m
    = (profit + gas) - gas*m, so no need to recompute proceeds/repaid for each multiplier.
    Returns None if profitability can't be computed for any event (no gas/exit-cost data) or
    never drops to <=50% within the swept range.
    """
    base = [(e["profit_usd"], e["gas_usd"]) for e in economics_list if e["gas_usd"] is not None and e["profit_usd"] is not None]
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
    valid_delays = sorted(d for d in delay_samples if d is not None)
    stats = profitability_stats(valid)
    return {
        "n_events": len(events),
        "n_insolvent": sum(1 for e in valid if e["is_insolvent"]),
        **stats,
        "total_left_on_table_usd": sum(e["bad_debt_usd"] for e in valid),
        "median_delay_hours": statistics.median(valid_delays) if valid_delays else None,
        "n_delay_sampled": len(delay_samples),
        "n_delay_found": len(valid_delays),
        "walk_away_gas_multiplier_observed": walk_away_breakpoint(valid),
    }
