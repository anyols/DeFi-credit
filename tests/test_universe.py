
from kvr.universe import verify_market_id


def test_verify_market_id_wbtc_market():
    # Real data pulled from api.morpho.org / verified on-chain for the USDC/WBTC market, one of
    # the universe's top-15 markets by supply.
    ok = verify_market_id(
        "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "0xDddd770BADd886dF3864029e4B377B5F6a2B6b83",
        "0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC",
        860000000000000000,
    )
    assert ok


def test_verify_market_id_rejects_tampered_params():
    ok = verify_market_id(
        "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "0xDddd770BADd886dF3864029e4B377B5F6a2B6b83",
        "0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC",
        900000000000000000,  # wrong lltv
    )
    assert not ok
