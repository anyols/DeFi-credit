"""T8/T9 (Task 4, SPEC.md section 5's own numbering continued): validation tests for
liq_edge.py's reconstruction math, kept chain-free and deterministic like T7's toy cases --
the search algorithm and the breakeven formula are tested on their own, not against a live RPC.
"""

import pytest

from kvr import liq_edge
from kvr import morpho_math as mm


def test_t8_breakeven_price_matches_max_borrow_hand_computed():
    # 1 whole 18-decimal token (collateral_raw=1e18), lltv=80%, debt=$800 (USDC, 6 decimals).
    # max_borrow(collateral, price, lltv) == debt at price=1e27 (verified by hand: this price
    # implies $1000/token, and max_borrow at that price is exactly 800 USDC raw units * 1e6).
    collateral_raw = 10**18
    lltv_raw = int(0.8 * mm.WAD)
    debt_raw = 800 * 10**6

    price = liq_edge.breakeven_price(debt_raw, collateral_raw, lltv_raw)
    assert price == pytest.approx(1e27, rel=1e-12)

    # Round-trip through the real protocol formula: max_borrow at this price should equal debt
    # (up to integer-vs-float rounding, since breakeven_price is deliberately float/analytics).
    recovered = mm.max_borrow(collateral_raw, int(price), lltv_raw)
    assert recovered == pytest.approx(debt_raw, rel=1e-9)


def test_t8_breakeven_price_none_for_zero_collateral_or_lltv():
    assert liq_edge.breakeven_price(100, 0, int(0.8 * mm.WAD)) is None
    assert liq_edge.breakeven_price(100, 10**18, 0) is None


def test_t9_search_delay_hours_finds_a_synthetic_breach_with_few_calls():
    # Breached (500) from execution (h=0) back through h=19, healthy (1000) from h=20 onward --
    # i.e. this position had been liquidatable for 19 hours before it was actually liquidated.
    # Breakeven is 600, so the search should land exactly on hour=19 (see liq_edge.py's own
    # docstring for why h=0 is checked first, not assumed).
    breakeven = 600
    calls = []

    def price_at_hours_back(h):
        calls.append(h)
        return 500 if h < 20 else 1000

    result = liq_edge._search_delay_hours(price_at_hours_back, max_hours=168, breakeven_px=breakeven)
    assert result == 19
    # O(log(max_hours)), not a linear 168-hour scan: exponential phase (1,2,4,8,16,32 -> ~6
    # calls, plus the initial h=0 check) plus a small binary refine inside the (16,32) bracket
    # (~4 more) -- well under 20.
    assert len(calls) < 15


def test_t9_search_delay_hours_handles_a_same_block_breach():
    # Breached only at the execution block itself (h=0), healthy by h=1 onward -- a real pattern
    # (a price-feed update landing in the same block as the liquidation it triggers), found by
    # hand on an actual WBTC liquidation. Delay should be 0, not "predates window".
    breakeven = 600

    def price_at_hours_back(h):
        return 500 if h == 0 else 1000

    assert liq_edge._search_delay_hours(price_at_hours_back, max_hours=168, breakeven_px=breakeven) == 0


def test_t9_search_delay_hours_none_when_execution_block_itself_is_not_breached():
    # If even h=0 isn't breached, something is inconsistent (a real liquidation implies the
    # position WAS unhealthy at execution) -- must return None, not a fabricated delay.
    def never_breached(h):
        return 1000

    assert liq_edge._search_delay_hours(never_breached, max_hours=168, breakeven_px=600) is None


def test_t9_search_delay_hours_lower_bound_when_breach_holds_to_window_edge():
    # Breached at every probed hour, all the way back to max_hours -- the true breach could
    # extend further; max_hours itself is reported as a lower bound, not a fabricated exact value.
    def always_breached(h):
        return 500

    result = liq_edge._search_delay_hours(always_breached, max_hours=48, breakeven_px=600)
    assert result == 48


def test_t9_walk_away_breakpoint_hand_computed():
    # Two events, gas=$10 each. profit' = (profit+gas) - gas*m, flips to <=0 at m = (profit+gas)/gas.
    # Event A: profit=$50 -> flips at m=6. Event B: profit=$6 -> flips at m=1.6.
    # At m=1.5: A's profit'=60-15=45>0, B's profit'=16-15=1>0 -- both still profitable (share=1.0).
    # At m=2: A's profit'=60-20=40>0, B's profit'=16-20=-4<=0 -- 1 of 2 (50%) profitable, the
    # first multiplier in the swept list where share<=0.5, so that's the reported breakpoint.
    economics = [
        {"profit_dex_usd": 50.0, "profit_mm_usd": None, "gas_usd": 10.0},
        {"profit_dex_usd": 6.0, "profit_mm_usd": None, "gas_usd": 10.0},
    ]
    assert liq_edge.walk_away_breakpoint(economics, multipliers=(1, 1.5, 2, 3)) == 2


def test_t9_walk_away_breakpoint_none_without_gas_or_profit_data():
    assert liq_edge.walk_away_breakpoint([]) is None
    no_gas = [{"profit_dex_usd": 50.0, "profit_mm_usd": None, "gas_usd": None}]
    assert liq_edge.walk_away_breakpoint(no_gas) is None
