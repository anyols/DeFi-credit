"""build_report.usd_to_collateral_raw: hand-computed check that inverting Morpho's oracle price
convention (collateral_raw * price // 1e36 == loan_raw, loan token always USDC/1e6) gives the
right raw collateral size for a target USD notional, without needing the collateral's decimals.
"""

import build_report


def test_usd_to_collateral_raw_matches_hand_computed_wbtc_case():
    # 8-decimal collateral priced at $65,000/whole token: price = 65000 * 1e36 / 10**(8-6) = 6.5e38.
    price = 6.5 * 10**38
    raw = build_report.usd_to_collateral_raw(65_000, price)
    assert raw == 10**8  # exactly 1 whole WBTC-like token (1e8 raw units)
