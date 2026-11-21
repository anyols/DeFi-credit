"""M12: Stream Finance / xUSD backtest (Nov 2025). Scope note (SPEC.md section 7's own cut
list, applied given time constraints): reduced to oracle class + concentration metrics only,
compared against realized bad debt -- the first two of the three things SPEC.md's own fallback
explicitly keeps.

Runs against Arbitrum (the xUSD market with real size lives there, not Ethereum), via
Arbitrum's public RPC -- a separate, ad hoc path from chain.py's Ethereum-only config.RPC_URL,
reusing chain.py's pure ABI encode/decode helpers since those are chain-agnostic.
"""

from web3 import Web3

from . import api, chain, oracle

ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"
XUSD_MARKET_ID = "0x9e90aec7d768403dacc9dd0d8320307fda3f980eed4df43e3e52168a1c667709"
PRE_ANNOUNCEMENT_TIMESTAMP = 1762041600  # 2025-11-02 00:00:00 UTC

_arb_w3 = None


def arb_w3():
    global _arb_w3
    if _arb_w3 is None:
        _arb_w3 = Web3(Web3.HTTPProvider(ARBITRUM_RPC))
    return _arb_w3


def arb_call(to, fn_name, args, block):
    data = chain.encode_call(fn_name, args)
    raw = arb_w3().eth.call({"to": Web3.to_checksum_address(to), "data": data}, block_identifier=block)
    return chain.decode_result(fn_name, raw)


def find_block_by_timestamp(target_ts, w3=None):
    """Binary search over block numbers for the first block at or after target_ts."""
    w3 = w3 or arb_w3()
    lo, hi = 1, w3.eth.block_number
    while lo < hi:
        mid = (lo + hi) // 2
        ts = w3.eth.get_block(mid)["timestamp"]
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def classify_oracle_arbitrum(oracle_address, block):
    """Same classification logic as oracle.py, retargeted at Arbitrum via arb_call."""
    fields = {}
    for fn in oracle._FEED_FIELDS + oracle._VAULT_FIELDS:
        try:
            (value,) = arb_call(oracle_address, fn, [], block)
            fields[fn] = value
        except Exception:
            fields[fn] = None

    if all(v is None for v in fields.values()):
        return {"class": "UNKNOWN", "fields": fields, "feeds": {}}
    feeds = {k: fields[k] for k in oracle._FEED_FIELDS}
    has_feed = any(oracle._is_set(v) for v in feeds.values())
    has_vault = any(oracle._is_set(fields[k]) for k in oracle._VAULT_FIELDS)
    cls = "MARKET" if has_feed else ("EXCHANGE_RATE" if has_vault else "FIXED")

    descriptions = {}
    for k, v in feeds.items():
        if not oracle._is_set(v):
            continue
        try:
            (desc,) = arb_call(v, "description", [], block)
            descriptions[k] = desc
        except Exception:
            pass
    return {"class": cls, "fields": fields, "feeds": descriptions}


def concentration_metrics(market_id):
    """Top-1/top-5 borrower share of debt and HHI, from current API-indexed positions (a
    snapshot proxy, not a true point-in-time-at-2025-11-02 read -- see module docstring)."""
    positions = api.get_market_positions(market_id, borrowers_only=True)
    debts = sorted((int(p["state"]["borrowAssets"]) for p in positions if p["state"]["borrowAssets"]), reverse=True)
    total = sum(debts)
    if total == 0 or not debts:
        return {"n_borrowers": len(debts), "top1_share": None, "top5_share": None, "hhi": None}
    shares = [d / total for d in debts]
    hhi = sum(s**2 for s in shares)
    return {
        "n_borrowers": len(debts),
        "top1_share": shares[0],
        "top5_share": sum(shares[:5]),
        "hhi": hhi,
    }


def realized_bad_debt(market_id, chain_id=42161):
    """Both the API's badDebt (current mark-to-market shortfall estimate) and realizedBadDebt
    (what's actually been written off via a Liquidate event's bad-debt path) -- these can and do
    diverge when most of a defunct market's debt is still sitting un-liquidated, not yet
    crystallized into an on-chain loss. Report both rather than silently picking one.
    """
    liqs = api.get_market_liquidations(market_id, limit=200)
    sum_from_liquidations = sum(int(l["data"]["badDebtAssets"]) for l in liqs)

    q = """
    query($id: String!, $chainId: Int!) {
      marketById(marketId: $id, chainId: $chainId) {
        state { borrowAssetsUsd }
        badDebt { underlying usd }
        realizedBadDebt { underlying usd }
      }
    }
    """
    data = api.graphql(q, {"id": market_id, "chainId": chain_id})["marketById"]
    return {
        "n_liquidations": len(liqs),
        "sum_bad_debt_from_liquidation_events": sum_from_liquidations,
        "current_borrow_assets_usd": data["state"]["borrowAssetsUsd"],
        "current_mark_to_market_bad_debt_usd": data["badDebt"]["usd"],
        "realized_bad_debt_usd": data["realizedBadDebt"]["usd"],
    }
