"""RPC access to Morpho Blue: single calls, Multicall3 batching, chunked getLogs, parquet caching.

ABI fragments below are hand-verified against morpho-org/morpho-blue src/interfaces/IMorpho.sol
(IMorphoStaticTyping forms) rather than taken from memory, per SPEC.md ground rules.
"""

import time
from pathlib import Path

import pandas as pd
import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
from web3 import Web3

import config

MULTICALL3 = to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")

# name -> (solidity signature, input types, output types). Verified against
# morpho-org/morpho-blue src/interfaces/{IMorpho,IIrm}.sol, IMorphoStaticTyping forms.
_MARKET_PARAMS_T = "(address,address,address,address,uint256)"
_MARKET_T = "(uint128,uint128,uint128,uint128,uint128,uint128)"

FUNCTIONS = {
    "market": ("market(bytes32)", ["bytes32"], ["uint128"] * 6),
    "position": ("position(bytes32,address)", ["bytes32", "address"], ["uint256", "uint128", "uint128"]),
    "idToMarketParams": (
        "idToMarketParams(bytes32)",
        ["bytes32"],
        ["address", "address", "address", "address", "uint256"],
    ),
    "borrowRateView": (
        f"borrowRateView({_MARKET_PARAMS_T},{_MARKET_T})",
        [_MARKET_PARAMS_T, _MARKET_T],
        ["uint256"],
    ),
    "rateAtTarget": ("rateAtTarget(bytes32)", ["bytes32"], ["int256"]),
    "price": ("price()", [], ["uint256"]),
    # MorphoChainlinkOracleV2 fields (morpho-org/morpho-blue-oracles), each a zero address when
    # that oracle doesn't use that slot. Not every Morpho oracle is this contract -- see oracle.py.
    "BASE_FEED_1": ("BASE_FEED_1()", [], ["address"]),
    "BASE_FEED_2": ("BASE_FEED_2()", [], ["address"]),
    "QUOTE_FEED_1": ("QUOTE_FEED_1()", [], ["address"]),
    "QUOTE_FEED_2": ("QUOTE_FEED_2()", [], ["address"]),
    "BASE_VAULT": ("BASE_VAULT()", [], ["address"]),
    "QUOTE_VAULT": ("QUOTE_VAULT()", [], ["address"]),
    "description": ("description()", [], ["string"]),
    # Aave V3 Pool (aave-v3-core IPool.sol / DataTypes.sol). A struct-returning function is ABI
    # encoded as its fields inlined in order, so this flat type list decodes it directly.
    "getReserveData": (
        "getReserveData(address)",
        ["address"],
        ["uint256", "uint128", "uint128", "uint128", "uint128", "uint128", "uint40", "uint16",
         "address", "address", "address", "address", "uint128", "uint128", "uint128"],
    ),
    # Uniswap V3 QuoterV2 (v3-periphery IQuoterV2.sol). Not a view function on-chain (it reverts
    # internally to compute the result), but eth_call works fine since we never send a real tx.
    "quoteExactInputSingle": (
        "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
        ["(address,address,uint256,uint24,uint160)"],
        ["uint256", "uint160", "uint32", "uint256"],
    ),
}

_w3 = None


def w3():
    global _w3
    if _w3 is None:
        if not config.RPC_URL:
            raise RuntimeError("RPC_URL is not set. Copy .env.example to .env and fill in an archive RPC URL.")
        _w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    return _w3


def encode_call(fn_name, args):
    sig, in_types, _ = FUNCTIONS[fn_name]
    selector = function_signature_to_4byte_selector(sig)
    return selector + abi_encode(in_types, args)


def decode_result(fn_name, data):
    _, _, out_types = FUNCTIONS[fn_name]
    return abi_decode(out_types, data)


def call(to, fn_name, args, block):
    """Single eth_call against any contract. Returns a decoded tuple."""
    data = encode_call(fn_name, args)
    raw = w3().eth.call({"to": Web3.to_checksum_address(to), "data": data}, block_identifier=block)
    return decode_result(fn_name, raw)


def call_morpho(fn_name, args, block):
    """Single eth_call against the Morpho Blue singleton. Returns a decoded tuple."""
    return call(config.MORPHO_BLUE, fn_name, args, block)


_AGGREGATE3_SIG = "aggregate3((address,bool,bytes)[])"
_AGGREGATE3_SELECTOR = function_signature_to_4byte_selector(_AGGREGATE3_SIG)


def multicall(calls, block, batch_size=300):
    """calls: list of (to, fn_name, args) against any contracts.

    Returns a list the same length as `calls`, each entry either a decoded tuple or None
    (None means that single call reverted -- callers decide whether that's fatal).
    """
    results = []
    for start in range(0, len(calls), batch_size):
        batch = calls[start : start + batch_size]
        encoded = [(to, True, encode_call(fn, args)) for to, fn, args in batch]
        call_data = _AGGREGATE3_SELECTOR + abi_encode(["(address,bool,bytes)[]"], [encoded])
        raw = w3().eth.call({"to": MULTICALL3, "data": call_data}, block_identifier=block)
        decoded = abi_decode(["(bool,bytes)[]"], raw)[0]
        for (_to, fn, _args), (success, ret) in zip(batch, decoded):
            results.append(decode_result(fn, ret) if success and len(ret) > 0 else None)
    return results


def multicall_morpho(calls, block, batch_size=300):
    """calls: list of (fn_name, args) against the Morpho Blue singleton specifically."""
    return multicall([(config.MORPHO_BLUE, fn, args) for fn, args in calls], block, batch_size)


def get_logs_chunked(address, topics, from_block, to_block, chunk_size=10, max_retries=5):
    """Yield raw logs over [from_block, to_block] in chunks of `chunk_size` blocks.

    chunk_size defaults to 10 to match free-tier RPC limits (e.g. Alchemy free tier caps
    eth_getLogs at 10 blocks on Ethereum mainnet). Raise chunk_size on a paid plan.
    """
    block = from_block
    while block <= to_block:
        end = min(block + chunk_size - 1, to_block)
        attempt = 0
        while True:
            try:
                logs = w3().eth.get_logs(
                    {"address": address, "topics": topics, "fromBlock": block, "toBlock": end}
                )
                break
            except Exception:
                attempt += 1
                if attempt > max_retries:
                    raise
                time.sleep(min(2**attempt, 30))
        for log in logs:
            yield log
        block = end + 1


def cache_parquet(path, fetch_fn):
    """Return a cached DataFrame at `path` if present, else call fetch_fn() and cache the result."""
    path = Path(path)
    if path.exists():
        return pd.read_parquet(path)
    df = fetch_fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df
