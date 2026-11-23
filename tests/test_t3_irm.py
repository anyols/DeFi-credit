
import pytest

import config
from kvr import chain, irm, universe

pytestmark = pytest.mark.skipif(
    not config.RPC_URL or not config.SNAPSHOT_BLOCK,
    reason="RPC_URL/SNAPSHOT_BLOCK not set; see .env.example",
)


def test_t3_borrow_rate_matches_onchain_for_every_market():
    block = config.SNAPSHOT_BLOCK
    block_ts = chain.w3().eth.get_block(block)["timestamp"]
    markets = universe.build_markets()

    for market_id in markets["market_id"]:
        mid = bytes.fromhex(market_id[2:])
        raw_market = chain.call_morpho("market", [mid], block)
        total_supply_assets, _ts_shares, total_borrow_assets, _tb_shares, last_update, _fee = raw_market
        params = chain.call_morpho("idToMarketParams", [mid], block)
        irm_addr = params[3]
        if irm_addr.lower() == "0x0000000000000000000000000000000000000000":
            continue  # Morpho's idle market has no IRM at all

        (stored_rate_at_target,) = chain.call(irm_addr, "rateAtTarget", [mid], block)
        elapsed = block_ts - last_update
        u = irm.utilization(total_borrow_assets, total_supply_assets)

        my_rate, _end_rt = irm.borrow_rate(u, stored_rate_at_target, elapsed)
        (onchain_rate,) = chain.call(irm_addr, "borrowRateView", [params, raw_market], block)

        if onchain_rate == 0:
            assert my_rate == 0
            continue
        rel_err = abs(my_rate - onchain_rate) / onchain_rate
        assert rel_err < 1e-9, f"{market_id}: mine={my_rate} onchain={onchain_rate}"
