"""Morpho Blue's own math, replicated integer-exact. Every formula here is quoted/verified
against morpho-org/morpho-blue source (MIT), not reconstructed from the spec's prose:
- share<->asset conversions: src/libraries/SharesMathLib.sol
- health check, maxBorrow: src/Morpho.sol::_isHealthy
- LIF, liquidation seize/repay conversions, bad debt: src/Morpho.sol::liquidate +
  src/libraries/ConstantsLib.sol

Originally slated as a hand-written file (see SPEC.md 1.1); written by Claude Code instead at
the user's explicit request to reallocate that ownership for this run, after flagging the
tradeoff (SPEC.md section 8's whiteboard questions assume hand-derivation). Every formula below
is still tested against real on-chain data and hand-computed toy cases (tests/test_toy.py,
tests/test_t1_positions.py), not just transcribed.
"""

WAD = 10**18
ORACLE_PRICE_SCALE = 10**36
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1

LIQUIDATION_CURSOR = 3 * 10**17  # 0.3e18
MAX_LIQUIDATION_INCENTIVE_FACTOR = 115 * 10**16  # 1.15e18


def mul_div_down(a, b, c):
    return (a * b) // c


def mul_div_up(a, b, c):
    return (a * b + c - 1) // c


def w_mul_down(a, b):
    return mul_div_down(a, b, WAD)


def w_div_down(a, b):
    return mul_div_down(a, WAD, b)


def w_div_up(a, b):
    return mul_div_up(a, WAD, b)


def to_shares_down(assets, total_assets, total_shares):
    return mul_div_down(assets, total_shares + VIRTUAL_SHARES, total_assets + VIRTUAL_ASSETS)


def to_assets_down(shares, total_assets, total_shares):
    return mul_div_down(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def to_shares_up(assets, total_assets, total_shares):
    return mul_div_up(assets, total_shares + VIRTUAL_SHARES, total_assets + VIRTUAL_ASSETS)


def to_assets_up(shares, total_assets, total_shares):
    return mul_div_up(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def max_borrow(collateral, price, lltv):
    """price is the oracle price (collateral quoted in loan token, ORACLE_PRICE_SCALE=1e36)."""
    return mul_div_down(collateral * price // ORACLE_PRICE_SCALE, lltv, WAD)


def borrowed(borrow_shares, total_borrow_assets, total_borrow_shares):
    return to_assets_up(borrow_shares, total_borrow_assets, total_borrow_shares)


def is_healthy(collateral, price, lltv, borrow_shares, total_borrow_assets, total_borrow_shares):
    debt = borrowed(borrow_shares, total_borrow_assets, total_borrow_shares)
    if debt == 0:
        return True
    return debt <= max_borrow(collateral, price, lltv)


def health_factor(collateral, price, lltv, borrow_shares, total_borrow_assets, total_borrow_shares):
    debt = borrowed(borrow_shares, total_borrow_assets, total_borrow_shares)
    if debt == 0:
        return float("inf")
    return max_borrow(collateral, price, lltv) / debt


def liquidation_incentive_factor(lltv):
    """LIF = min(1.15, 1 / (1 - 0.3*(1-lltv))) = min(1.15, 1/(0.7 + 0.3*lltv)). Morpho.sol::liquidate."""
    denom = WAD - w_mul_down(LIQUIDATION_CURSOR, WAD - lltv)
    return min(MAX_LIQUIDATION_INCENTIVE_FACTOR, w_div_down(WAD, denom))


def repaid_shares_for_seized(seized_assets, price, lif, total_borrow_assets, total_borrow_shares):
    """Given seizedAssets (liquidator's chosen input), the repaidShares Morpho computes."""
    seized_quoted = mul_div_up(seized_assets, price, ORACLE_PRICE_SCALE)
    return to_shares_up(w_div_up(seized_quoted, lif), total_borrow_assets, total_borrow_shares)


def seized_for_repaid_shares(repaid_shares, price, lif, total_borrow_assets, total_borrow_shares):
    """Given repaidShares (liquidator's chosen input), the seizedAssets Morpho computes."""
    repaid_assets = to_assets_down(repaid_shares, total_borrow_assets, total_borrow_shares)
    return mul_div_down(w_mul_down(repaid_assets, lif), ORACLE_PRICE_SCALE, price)


def apply_liquidation(
    collateral, borrow_shares, price, lltv, total_borrow_assets, total_borrow_shares, repaid_shares=None, seized_assets=None
):
    """One liquidation call, given exactly one of repaidShares or seizedAssets as the
    liquidator's input (mirrors Morpho.sol::liquidate's two branches exactly -- a liquidator who
    knows collateral won't cover a full close passes seizedAssets=collateral instead).
    Returns (new_collateral, new_borrow_shares, seized_assets, repaid_assets, bad_debt_shares,
    bad_debt_assets, new_total_borrow_assets, new_total_borrow_shares).
    """
    assert (repaid_shares is None) != (seized_assets is None), "pass exactly one of repaid_shares, seized_assets"
    lif = liquidation_incentive_factor(lltv)
    if seized_assets is None:
        seized_assets = seized_for_repaid_shares(repaid_shares, price, lif, total_borrow_assets, total_borrow_shares)
    else:
        repaid_shares = repaid_shares_for_seized(seized_assets, price, lif, total_borrow_assets, total_borrow_shares)
    repaid_assets = to_assets_up(repaid_shares, total_borrow_assets, total_borrow_shares)

    new_borrow_shares = borrow_shares - repaid_shares
    new_total_borrow_shares = total_borrow_shares - repaid_shares
    new_total_borrow_assets = max(0, total_borrow_assets - repaid_assets)
    new_collateral = collateral - seized_assets

    bad_debt_shares = 0
    bad_debt_assets = 0
    if new_collateral == 0:
        bad_debt_shares = new_borrow_shares
        bad_debt_assets = min(new_total_borrow_assets, to_assets_up(bad_debt_shares, new_total_borrow_assets, new_total_borrow_shares))
        new_total_borrow_assets -= bad_debt_assets
        new_total_borrow_shares -= bad_debt_shares
        new_borrow_shares = 0

    return (
        new_collateral,
        new_borrow_shares,
        seized_assets,
        repaid_assets,
        bad_debt_shares,
        bad_debt_assets,
        new_total_borrow_assets,
        new_total_borrow_shares,
    )
