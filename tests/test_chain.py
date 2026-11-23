
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from kvr import chain


def test_aggregate3_selector_matches_known_value():
    # 0x82ad56cb is Multicall3's well-known aggregate3((address,bool,bytes)[]) selector.
    assert chain._AGGREGATE3_SELECTOR.hex() == "82ad56cb"


def test_market_call_roundtrip():
    call = chain.encode_call("market", [b"\x01" * 32])
    assert len(call) == 36  # 4-byte selector + one bytes32 arg
    fake_return = abi_encode(["uint128"] * 6, [1, 2, 3, 4, 5, 6])
    assert chain.decode_result("market", fake_return) == (1, 2, 3, 4, 5, 6)


def test_multicall_payload_roundtrip():
    to = chain.MULTICALL3
    call = chain.encode_call("market", [b"\x02" * 32])
    encoded = [(to, True, call)]
    packed = chain._AGGREGATE3_SELECTOR + abi_encode(["(address,bool,bytes)[]"], [encoded])
    args_back = abi_decode(["(address,bool,bytes)[]"], packed[4:])
    assert args_back[0][0][0].lower() == to.lower()
    assert args_back[0][0][1] is True
    assert args_back[0][0][2] == call
