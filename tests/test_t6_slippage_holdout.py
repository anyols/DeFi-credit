

import pytest

import config
from kvr import dex

pytestmark = pytest.mark.skipif(not config.RPC_URL, reason="RPC_URL not set; see .env.example")

WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


@pytest.mark.xfail(reason="power-law fit doesn't hold to 15% on this pool's real, uneven depth -- see module docstring", strict=True)
def test_t6_fit_predicts_held_out_size():
    block = config.SNAPSHOT_BLOCK
    fit_sizes_btc = [0.5, 2.5, 50]  # interpolation: both smaller and larger than the holdout
    held_out_btc = 10

    sizes_raw = [int(s * 10**8) for s in sorted(fit_sizes_btc + [held_out_btc])]
    points = dex.quote_curve(WBTC, USDC, sizes_raw, block)

    fit_points = [p for p in points if p[0] in {int(s * 10**8) for s in fit_sizes_btc}]
    fit = dex.fit_slippage(fit_points)
    assert fit is not None
    a, b, _depth_cap = fit

    held_point = next(p for p in points if p[0] == int(held_out_btc * 10**8))
    baseline_rate = fit_points[0][1] / fit_points[0][0]
    actual_rate = held_point[1] / held_point[0]
    actual_slip = 1 - actual_rate / baseline_rate
    predicted_slip = dex.predict_slippage(a, b, held_point[0])

    rel_err = abs(predicted_slip - actual_slip) / actual_slip
    assert rel_err < 0.15, f"predicted={predicted_slip} actual={actual_slip} rel_err={rel_err}"
