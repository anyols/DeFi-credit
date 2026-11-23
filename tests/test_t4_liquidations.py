

import pytest

import config
from kvr import api, chain, oracle
from kvr import morpho_math as mm

pytestmark = pytest.mark.skipif(not config.RPC_URL, reason="RPC_URL not set; see .env.example")

# USDC/WBTC -- one of the universe's top-15 markets by supply, picked because it has real,
# plentiful liquidation history.
MARKET_ID = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
ORACLE = "0xDddd770BADd886dF3864029e4B377B5F6a2B6b83"
LLTV = 860000000000000000


def test_t4_replay_last_50_liquidations():
    lif = mm.liquidation_incentive_factor(LLTV)
    block_ts = chain.w3().eth.get_block(config.SNAPSHOT_BLOCK)["timestamp"]
    liqs = api.get_market_liquidations(MARKET_ID, limit=50, before_timestamp=block_ts)
    assert len(liqs) > 0

    for liq in liqs:
        block = liq["blockNumber"]
        price = oracle.read_price(ORACLE, block)
        repaid = int(liq["data"]["repaidAssets"])
        seized_actual = int(liq["data"]["seizedAssets"])
        seized_expected = mm.mul_div_down(mm.w_mul_down(repaid, lif), 10**36, price)

        rel_err = abs(seized_actual - seized_expected) / seized_actual
        assert rel_err < 1e-6, f"{liq['txHash']}: actual={seized_actual} expected={seized_expected}"
