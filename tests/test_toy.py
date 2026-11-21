"""T7 (SPEC.md section 5): hand-computed single-position markets, exact.

Each "expected" value below is computed independently in the test itself (plain integer
arithmetic spelled out, not by calling morpho_math's own helpers), so this isn't circular. WAD
constants are written as exact integer literals throughout (e.g. `115 * 10**16`, not
`int(1.15 * WAD)`) -- 1.15 isn't exactly representable in binary float, so the float form
silently rounds to 1149999999999999872, one off from the real 1.15e18. That's exactly the kind
of bug integer-exact math is supposed to catch.

Covers: healthy, liquidatable (profitable close), liquidatable into bad debt, and the
virtual-shares rounding mechanics on their own. The other two SPEC.md T7 cases --
liquidatable-unprofitable (needs stress.py's slippage/gas check) and fixed-oracle (needs
oracle.py's classification) -- move to test_stress.py / test_oracle.py once those exist.
"""

from kvr import morpho_math as mm

WAD = 10**18


def test_virtual_shares_rounding():
    # shares=1,000,000; totalAssets=2,000,000; totalShares=1,000,000.
    # toAssetsUp = ceil(1,000,000 * 2,000,001 / 2,000,000) = ceil(1,000,000.5) = 1,000,001.
    assert mm.to_assets_up(1_000_000, 2_000_000, 1_000_000) == 1_000_001
    # Rounding DOWN the same inputs must give the floor instead: 1,000,000.
    assert mm.to_assets_down(1_000_000, 2_000_000, 1_000_000) == 1_000_000


def test_healthy_position():
    lltv = 8 * 10**17  # 0.8
    price = 10**36  # 1:1
    collateral = 100_000_000_000  # 1e11
    total_borrow_assets = 49_999_999_999  # +1 = 5e10
    total_borrow_shares = 999_999_000_000  # +1e6 = 1e12
    borrow_shares = total_borrow_shares  # single position owns the whole pool

    # borrowed = ceil(999,999,000,000 * 50,000,000,000 / 1,000,000,000,000) = 49,999,950,000 (exact).
    expected_borrowed = 49_999_950_000
    assert mm.borrowed(borrow_shares, total_borrow_assets, total_borrow_shares) == expected_borrowed

    # maxBorrow = floor(100,000,000,000 * 1e36 / 1e36) * 0.8 = 80,000,000,000.
    expected_max_borrow = 80_000_000_000
    assert mm.max_borrow(collateral, price, lltv) == expected_max_borrow

    assert expected_borrowed < expected_max_borrow
    assert mm.is_healthy(collateral, price, lltv, borrow_shares, total_borrow_assets, total_borrow_shares)
    hf = mm.health_factor(collateral, price, lltv, borrow_shares, total_borrow_assets, total_borrow_shares)
    assert hf == expected_max_borrow / expected_borrowed  # same division, done independently


def test_liquidatable_full_close_no_bad_debt():
    lltv = 8 * 10**17  # 0.8
    price = 10**36
    total_borrow_assets = 49_999_999_999
    total_borrow_shares = 999_999_000_000
    borrow_shares = total_borrow_shares
    collateral = 60_000_000_000  # maxBorrow=48,000,000,000 < borrowed=49,999,950,000 -> unhealthy

    expected_max_borrow = 48_000_000_000
    assert mm.max_borrow(collateral, price, lltv) == expected_max_borrow
    assert not mm.is_healthy(collateral, price, lltv, borrow_shares, total_borrow_assets, total_borrow_shares)

    # LIF = min(1.15, 1/(0.7 + 0.3*0.8)) = min(1.15, 1/0.94) = 1.0638297872340425... (uncapped).
    lif = mm.liquidation_incentive_factor(lltv)
    expected_lif = min(115 * 10**16, mm.w_div_down(WAD, WAD - mm.w_mul_down(mm.LIQUIDATION_CURSOR, WAD - lltv)))
    assert lif == expected_lif
    assert lif < 115 * 10**16  # below the cap for this lltv

    result = mm.apply_liquidation(
        collateral, borrow_shares, price, lltv, total_borrow_assets, total_borrow_shares, repaid_shares=borrow_shares
    )
    new_collateral, new_borrow_shares, seized_assets, repaid_assets, bad_debt_shares, bad_debt_assets, *_ = result

    # repaidAssets = borrowed = 49,999,950,000 (full close).
    assert repaid_assets == 49_999_950_000
    # seizedAssets = floor(repaidAssets_down * LIF / 1e18), price=1e36 cancels out.
    expected_seized = mm.mul_div_down(mm.w_mul_down(49_999_950_000, lif), 10**36, price)
    assert seized_assets == expected_seized
    assert seized_assets < collateral  # plenty of collateral left -- no bad debt
    assert new_collateral == collateral - expected_seized
    assert bad_debt_shares == 0 and bad_debt_assets == 0
    assert new_borrow_shares == 0  # fully closed


def test_liquidation_realizes_bad_debt_when_collateral_runs_out():
    lltv = 5 * 10**17  # 0.5 -> LIF saturates at the 1.15 cap: 1/(0.7+0.3*0.5)=1/0.85=1.176.. > 1.15
    price = 10**36
    total_borrow_assets = 49_999_999_999
    total_borrow_shares = 999_999_000_000
    borrow_shares = total_borrow_shares  # single position, borrowed = 49,999,950,000
    collateral = 40_000_000_000  # even at the LIF cap, 40e9/1.15 ~= 34.8e9 worth of debt < 49,999,950,000

    lif = mm.liquidation_incentive_factor(lltv)
    assert lif == 115 * 10**16  # capped

    # A liquidator seizing all remaining collateral (seizedAssets=collateral) when it's known a
    # full close isn't possible -- mirrors how a real liquidator would call Morpho.sol::liquidate.
    result = mm.apply_liquidation(
        collateral, borrow_shares, price, lltv, total_borrow_assets, total_borrow_shares, seized_assets=collateral
    )
    new_collateral, new_borrow_shares, seized, repaid_assets, bad_debt_shares, bad_debt_assets, new_tba, new_tbs = result

    # Independently verified (see conversation scratch calc): seizing all 40,000,000,000 of
    # collateral only covers 695,652,173,920 of the position's 999,999,000,000 borrow shares.
    assert seized == collateral
    assert new_collateral == 0
    assert repaid_assets == 34_782_608_696
    assert new_borrow_shares == 0  # bad debt zeroes out the position
    assert bad_debt_shares == 304_346_826_080  # the remaining 999,999,000,000 - 695,652,173,920
    assert bad_debt_assets == 15_217_341_304  # slightly under the pool's remaining 15,217,391,303
    assert new_tba == 49_999  # 15,217,391,303 - 15,217,341,304: a tiny unrealizable remainder
    assert new_tbs == 0  # all remaining shares were wiped out as bad debt
