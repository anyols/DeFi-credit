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


def test_encode_path_matches_hand_computed_packed_bytes():
    # abi.encodePacked(tokenA, fee0, tokenB, fee1, tokenC) -- 20-byte address + 3-byte big-endian
    # fee per hop, no padding/offsets (NOT standard ABI encoding, which this must not match).
    token_a = "0x" + "11" * 20
    token_b = "0x" + "22" * 20
    token_c = "0x" + "33" * 20
    path = dex._encode_path([token_a, token_b, token_c], [500, 3000])
    expected = bytes([0x11] * 20) + (500).to_bytes(3, "big") + bytes([0x22] * 20) + (3000).to_bytes(3, "big") + bytes([0x33] * 20)
    assert path == expected
    assert len(path) == 20 + 3 + 20 + 3 + 20


def test_deeper_fit_picks_the_larger_depth_cap():
    # Two power-law curves with the same shape but different depth_cap -- deeper_fit must pick
    # whichever source's fit can actually absorb more size, not just whichever came first.
    def curve(sizes, baseline_q):
        return [(baseline_q, baseline_q, 500)] + [(q, round(q * (1 - 1e-20 * q**0.5)), 500) for q in sizes[1:]]

    shallow = curve([10**20, 4 * 10**20, 9 * 10**20], 10**20)
    deep = curve([10**20, 4 * 10**20, 9 * 10**20, 16 * 10**20, 25 * 10**20], 10**20)

    result = dex.deeper_fit(shallow, deep)
    assert result is not None
    source, fit = result
    assert source == "aggregator"  # deeper_fit's second argument is labeled "aggregator"
    assert fit[2] == 25 * 10**20

    # And the reverse: swapping arguments should pick the (now-first) deeper one as "onchain".
    result2 = dex.deeper_fit(deep, shallow)
    assert result2[0] == "onchain"
    assert result2[1][2] == 25 * 10**20


def test_deeper_fit_falls_back_when_only_one_source_has_a_fit():
    assert dex.deeper_fit([], []) is None
    only_aggregator = [(10**20, 10**20, 500), (4 * 10**20, 3 * 10**20, 500), (9 * 10**20, 5 * 10**20, 500)]
    result = dex.deeper_fit([], only_aggregator)
    assert result[0] == "aggregator"
