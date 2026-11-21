"""Off-chain half of M1's id check -- hits the live Morpho API (no RPC key needed)."""

from kvr.universe import verify_market_id


def test_verify_market_id_snusd_market():
    # Real data pulled from api.morpho.org for the USDC/sNUSD market the Keyrock USDC vault
    # (config.VAULT) is actually exposed to.
    ok = verify_market_id(
        "0xae60b71b407e0517ead445b7113a7ffa07ea4a9379d526ade541a3e9ec777cb4",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "0x08EFCC2F3e61185D0EA7F8830B3FEc9Bfa2EE313",
        "0x28E82e7f25DbcD487af27C80DE4F62553260fecA",
        "0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC",
        915000000000000000,
    )
    assert ok


def test_verify_market_id_rejects_tampered_params():
    ok = verify_market_id(
        "0xae60b71b407e0517ead445b7113a7ffa07ea4a9379d526ade541a3e9ec777cb4",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "0x08EFCC2F3e61185D0EA7F8830B3FEc9Bfa2EE313",
        "0x28E82e7f25DbcD487af27C80DE4F62553260fecA",
        "0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC",
        900000000000000000,  # wrong lltv
    )
    assert not ok
