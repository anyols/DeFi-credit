

import datetime as dt

import pytest

import config
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
        {"profit_usd": 50.0, "gas_usd": 10.0},
        {"profit_usd": 6.0, "gas_usd": 10.0},
    ]
    assert liq_edge.walk_away_breakpoint(economics, multipliers=(1, 1.5, 2, 3)) == 2


def test_t9_walk_away_breakpoint_none_without_gas_or_profit_data():
    assert liq_edge.walk_away_breakpoint([]) is None
    no_gas = [{"profit_usd": 50.0, "gas_usd": None}]
    assert liq_edge.walk_away_breakpoint(no_gas) is None


def test_t8_parse_pt_maturity_matches_pendle_naming_convention():
    assert liq_edge.parse_pt_maturity("PT-reUSD-10DEC2026") == dt.date(2026, 12, 10)
    assert liq_edge.parse_pt_maturity("PT-RLP-9APR2026") == dt.date(2026, 4, 9)
    assert liq_edge.parse_pt_maturity("wstETH") is None


def test_t8_exit_cost_perp_hedge_for_btc_eth_family():
    model, cost, note = liq_edge._exit_cost("WBTC", "BTC", seized_value_usd=100_000, event_timestamp=0, cost_of_capital_annual=0.05)
    assert model == "perp_hedge"
    assert cost == pytest.approx(100_000 * config.LIQ_EDGE_HEDGE_COST_BPS / 10_000)
    assert note is None


def test_t8_exit_cost_hold_to_maturity_for_pt_hand_computed():
    # Liquidated exactly 100 days before a maturity 36500 days (100 years) out would be absurd
    # to hand-verify in full, so pick a concrete, easy case: event at 2026-01-01, maturity
    # 2026-04-11 (100 days later), cost_of_capital=10%/yr -> carry = 100_000 * 0.10 * 100/365.
    ts = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    model, cost, note = liq_edge._exit_cost(
        "PT-fake-11APR2026", "PT (fixed maturity)", seized_value_usd=100_000, event_timestamp=ts, cost_of_capital_annual=0.10
    )
    assert model == "hold_to_maturity"
    assert cost == pytest.approx(100_000 * 0.10 * 100 / 365, rel=1e-6)
    assert "100d" in note


def test_t8_exit_cost_issuer_redemption_none_when_unverified():
    # RLP's cooldown/haircut are both None in config.py (unverified -- see its own docstring):
    # must report "not estimable", never a guessed cost.
    model, cost, note = liq_edge._exit_cost("RLP", "RWA/other", seized_value_usd=100_000, event_timestamp=0, cost_of_capital_annual=0.05)
    assert model == "issuer_redemption"
    assert cost is None
    assert "not verified" in note


def test_t8_exit_cost_issuer_redemption_computed_when_verified():
    # fxSAVE has cooldown=0, haircut=0 in config.py -- cost should be exactly 0, not None.
    model, cost, note = liq_edge._exit_cost("fxSAVE", "RWA/other", seized_value_usd=100_000, event_timestamp=0, cost_of_capital_annual=0.05)
    assert model == "issuer_redemption"
    assert cost == 0.0
    assert note is None


def test_t8_exit_cost_none_for_unmodeled_collateral():
    model, cost, note = liq_edge._exit_cost("AZND", "FIXED", seized_value_usd=100_000, event_timestamp=0, cost_of_capital_annual=0.05)
    assert model == "none"
    assert cost is None


def test_t9_profitability_stats_separates_dust_and_volume_weights():
    # 2 real events (one profitable, $900 volume; one not, $100 volume -- so volume-weighted
    # profitability is 900/1000=90%, but count-weighted is 1/2=50%) + 1 dust event excluded.
    economics = [
        {"is_dust": False, "profit_usd": 10.0, "repaid_usd": 900.0},
        {"is_dust": False, "profit_usd": -5.0, "repaid_usd": 100.0},
        {"is_dust": True, "profit_usd": -50.0, "repaid_usd": 5.0},
    ]
    stats = liq_edge.profitability_stats(economics)
    assert stats["n_dust"] == 1
    assert stats["dust_volume_usd"] == 5.0
    assert stats["n_priced"] == 2
    assert stats["profitable_share_count"] == pytest.approx(0.5)
    assert stats["profitable_share_volume"] == pytest.approx(0.9)
    assert stats["total_volume_usd"] == pytest.approx(1000.0)


def test_t9_decompose_hf_decline_hand_computed():
    # Collateral=1e18 (1 whole 18-decimal token), lltv=80%. At breach, HF=1.0 by construction:
    # debt_breach=800 (6-decimal USDC raw units *1e6 => 800e6) at price_breach=1e27 ($1000/token,
    # verified in test_t8_breakeven_price_matches_max_borrow_hand_computed). By execution, price
    # has dropped to $900/token (price_exec=0.9e27) with debt unchanged (no interest accrued) --
    # a purely price-driven decline: interest_driven_decline should be ~0, price_driven should
    # be the full ~10% (new HF = 900/1000 * old max_borrow / same debt = 0.9).
    collateral = 10**18
    lltv_raw = int(0.8 * mm.WAD)
    debt_breach = 800 * 10**6
    debt_exec = debt_breach  # no interest accrued in this hand-picked case
    price_breach = 1e27
    price_exec = 0.9e27

    result = liq_edge.decompose_hf_decline(collateral, lltv_raw, debt_breach, debt_exec, price_breach, price_exec)
    assert result["interest_driven_decline"] == pytest.approx(0.0, abs=1e-9)
    assert result["price_driven_decline"] == pytest.approx(0.1, rel=1e-6)
    assert result["label"] == "price-driven"
    assert result["price_change_pct"] == pytest.approx(-0.1, rel=1e-6)
    assert result["debt_growth_from_interest_usd"] == pytest.approx(0.0)
