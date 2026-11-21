"""Deterministic checks for spread.py's cross-section helpers (M10) -- no network needed."""

import numpy as np

from kvr import spread


def test_aave_morpho_funded_carry_exact():
    # 86% LTV, 3.79% Morpho supply vs 3.65% Aave borrow, hand-computed:
    # 0.86 * (0.0379 - 0.0365) = 0.86 * 0.0014 = 0.001204
    carry = spread.aave_morpho_funded_carry(0.86, 0.0379, 0.0365)
    assert abs(carry - 0.001204) < 1e-9


def test_aave_morpho_funded_carry_includes_collateral_yield():
    carry = spread.aave_morpho_funded_carry(0.8, 0.05, 0.03, collateral_yield=0.02)
    assert abs(carry - (0.02 + 0.8 * 0.02)) < 1e-9


def test_ols_regress_recovers_exact_linear_relationship():
    rng = np.random.default_rng(0)
    x1 = rng.uniform(0, 10, 50)
    x2 = rng.uniform(-5, 5, 50)
    y = 2.0 + 3.0 * x1 - 1.5 * x2  # no noise -- should recover exactly
    result = spread.ols_regress(y, np.column_stack([x1, x2]), ["x1", "x2"])
    assert abs(result["coefficients"]["intercept"] - 2.0) < 1e-8
    assert abs(result["coefficients"]["x1"] - 3.0) < 1e-8
    assert abs(result["coefficients"]["x2"] - (-1.5)) < 1e-8
    assert result["r_squared"] > 0.999999
    assert result["n"] == 50


def test_classify_collateral_buckets():
    assert spread.classify_collateral("WBTC") == "BTC"
    assert spread.classify_collateral("cbBTC") == "BTC"
    assert spread.classify_collateral("wstETH") == "ETH/LST"
    assert spread.classify_collateral("PT-sUSDE-26NOV2026") == "PT (fixed maturity)"
    assert spread.classify_collateral("USD3") == "stable/yield-bearing"
    assert spread.classify_collateral("PAXG") == "RWA/other"
    assert spread.classify_collateral(None) == "idle"
