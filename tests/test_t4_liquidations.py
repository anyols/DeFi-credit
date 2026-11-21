"""T4 (SPEC.md section 5): replay the market's real Liquidate events and check
seized ~= repaid * LIF * 1e36 / price(block-1), tolerance "rounding only". Uses the Morpho API
for the liquidation history (txHash/repaid/seized) and a live eth_call for price(block-1), so it
needs a connected archive RPC.
"""

import pytest

import config
from kvr import api, chain
from kvr import morpho_math as mm

pytestmark = pytest.mark.skipif(not config.RPC_URL, reason="RPC_URL not set; see .env.example")

# The USDC/sNUSD market the vault is actually exposed to -- picked because it has real
# liquidation history, unlike the vault's other (idle) markets.
MARKET_ID = "0xae60b71b407e0517ead445b7113a7ffa07ea4a9379d526ade541a3e9ec777cb4"
ORACLE = "0x28E82e7f25DbcD487af27C80DE4F62553260fecA"
LLTV = 915000000000000000


def test_t4_replay_last_50_liquidations():
    lif = mm.liquidation_incentive_factor(LLTV)
    liqs = api.get_market_liquidations(MARKET_ID, limit=50)
    assert len(liqs) > 0

    for liq in liqs:
        block = liq["blockNumber"]
        (price,) = chain.call(ORACLE, "price", [], block - 1)
        repaid = int(liq["data"]["repaidAssets"])
        seized_actual = int(liq["data"]["seizedAssets"])
        seized_expected = mm.mul_div_down(mm.w_mul_down(repaid, lif), 10**36, price)

        rel_err = abs(seized_actual - seized_expected) / seized_actual
        assert rel_err < 1e-6, f"{liq['txHash']}: actual={seized_actual} expected={seized_expected}"
