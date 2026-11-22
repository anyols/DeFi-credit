"""dex.fit_slippage: regression test for a real crash found while widening the universe past
WBTC/cbBTC (8 decimals, always well within int64) to 18-decimal, ~$1-priced collateral, where
raw quote sizes exceed int64 -- np.array() on such a list of plain Python ints silently makes an
object-dtype array, and np.log then raises "int object has no attribute log" instead of fitting.
"""

import pytest

from kvr import dex


def test_fit_slippage_handles_raw_sizes_beyond_int64():
    # A clean slip(q) = a*q^b power law (a=1e-20, b=0.5), with q well past int64's ~9.2e18 max --
    # this used to crash inside fit_slippage's np.log before q was cast to float.
    a, b = 1e-20, 0.5
    qs = [10**20, 4 * 10**20, 9 * 10**20, 16 * 10**20]
    # q0 (the smallest size) is fit_slippage's own baseline rate -- give it zero slip exactly,
    # so later points' slip relative to it matches the a*q^b model fit_slippage assumes.
    points = [(qs[0], qs[0], 500)] + [(q, round(q * (1 - a * q**b)), 500) for q in qs[1:]]
    assert all(q.bit_length() > 64 for q in qs)  # confirms this actually exercises the overflow path

    result = dex.fit_slippage(points)
    assert result is not None
    fit_a, fit_b, depth_cap = result
    assert fit_b == pytest.approx(b, rel=1e-3)
    assert fit_a == pytest.approx(a, rel=1e-2)
    assert depth_cap == qs[-1]
