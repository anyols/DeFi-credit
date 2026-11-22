"""T4 (SPEC.md section 5): replay the market's real Liquidate events and check
seized ~= repaid * LIF * 1e36 / price, tolerance "rounding only". Uses the Morpho API for the
liquidation history (txHash/repaid/seized) and a live eth_call for the oracle price, so it needs
a connected archive RPC.

SPEC.md's own text says price(block-1); empirically that is wrong for this market and was
caught by the global "an extreme/failing number is a bug until proven otherwise" rule, not
assumed away: 30 of 50 real liquidations failed at 1e-6 with price(block-1), all with
seized_actual > seized_expected. Checked the oracle price at block-1 vs the liquidation's own
block for the first failure (0x3d87329f..., block 26460969): it dropped 1.31% within that one
block, which is also exactly that event's relative error. Morpho's liquidate() calls
IOracle.price() synchronously in the same transaction, so the correct read is the liquidation's
own block, not block-1 -- confirmed by re-running all 50 events with that change: 0 mismatches.
"""

import pytest

import config
from kvr import api, chain
from kvr import morpho_math as mm

pytestmark = pytest.mark.skipif(not config.RPC_URL, reason="RPC_URL not set; see .env.example")

# USDC/WBTC -- one of the universe's top-15 markets by supply, picked because it has real,
# plentiful liquidation history.
MARKET_ID = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
ORACLE = "0xDddd770BADd886dF3864029e4B377B5F6a2B6b83"
LLTV = 860000000000000000


def test_t4_replay_last_50_liquidations():
    lif = mm.liquidation_incentive_factor(LLTV)
    liqs = api.get_market_liquidations(MARKET_ID, limit=50)
    assert len(liqs) > 0

    for liq in liqs:
        block = liq["blockNumber"]
        (price,) = chain.call(ORACLE, "price", [], block)
        repaid = int(liq["data"]["repaidAssets"])
        seized_actual = int(liq["data"]["seizedAssets"])
        seized_expected = mm.mul_div_down(mm.w_mul_down(repaid, lif), 10**36, price)

        rel_err = abs(seized_actual - seized_expected) / seized_actual
        assert rel_err < 1e-6, f"{liq['txHash']}: actual={seized_actual} expected={seized_expected}"
