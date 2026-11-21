"""T2 (SPEC.md section 5): HF for 20 random positions vs Morpho API, rel err < 1e-4.

Live comparison (current block, not config.SNAPSHOT_BLOCK): the API's `healthFactor` field is
computed against its own current indexed state, so matching it needs our on-chain read taken at
the same moment, not a pinned historical block.
"""

import random

import pytest

import config
from kvr import api, chain, oracle
from kvr import morpho_math as mm

pytestmark = pytest.mark.skipif(not config.RPC_URL, reason="RPC_URL not set; see .env.example")

# USDC/WBTC: real, active market with plenty of borrowers and a standard MarketParams/oracle.
MARKET_ID = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"


def test_t2_health_factor_matches_api():
    block = chain.w3().eth.block_number
    mid_bytes = bytes.fromhex(MARKET_ID[2:])

    params = chain.call_morpho("idToMarketParams", [mid_bytes], block)
    _loan_token, _collateral_token, oracle_addr, _irm, lltv = params
    total_supply_assets, _tss, total_borrow_assets, total_borrow_shares, _lu, _fee = chain.call_morpho(
        "market", [mid_bytes], block
    )
    price = oracle.read_price(oracle_addr, block)

    positions = api.get_market_positions(MARKET_ID, borrowers_only=True)
    # Dust positions (a few cents of debt) make rel_err meaningless: toAssetsUp's ceiling
    # rounding is +1 unit at most, which is 0.1%+ of a sub-cent debt but negligible on any real
    # position -- found via a real failure here (HF off by 0.1% on a $0.001-debt position).
    real_positions = [p for p in positions if int(p["state"]["borrowAssets"] or 0) > 10**6]
    assert len(real_positions) >= 20
    sample = random.sample(real_positions, 20)

    for p in sample:
        addr = p["user"]["address"]
        api_hf = p["healthFactor"]
        if api_hf is None:
            continue
        _supply_shares, borrow_shares, collateral = chain.call_morpho(
            "position", [mid_bytes, chain.Web3.to_checksum_address(addr)], block
        )
        my_hf = mm.health_factor(collateral, price, lltv, borrow_shares, total_borrow_assets, total_borrow_shares)
        rel_err = abs(my_hf - api_hf) / api_hf
        assert rel_err < 1e-4, f"{addr}: mine={my_hf} api={api_hf} rel_err={rel_err}"
