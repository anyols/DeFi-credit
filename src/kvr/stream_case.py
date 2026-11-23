"""M12: Stream Finance / xUSD backtest (Nov 2025). Scope note (SPEC.md section 7's own cut
list, applied given time constraints): reduced to oracle class + concentration metrics only,
compared against realized bad debt -- the first two of the three things SPEC.md's own fallback
explicitly keeps.

Runs against Arbitrum (the xUSD market with real size lives there, not Ethereum), via
Arbitrum's public RPC -- a separate, ad hoc path from chain.py's Ethereum-only config.RPC_URL,
reusing chain.py's pure ABI encode/decode helpers since those are chain-agnostic.
"""

import os

from web3 import Web3
from web3.exceptions import Web3RPCError
from requests.exceptions import RequestException

import config
from . import api, chain, oracle

ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"
XUSD_MARKET_ID = "0x9e90aec7d768403dacc9dd0d8320307fda3f980eed4df43e3e52168a1c667709"
PRE_ANNOUNCEMENT_TIMESTAMP = 1762041600  # 2025-11-02 00:00:00 UTC

_arb_w3 = None


def arb_w3():
    global _arb_w3
    if _arb_w3 is None:
        _arb_w3 = Web3(Web3.HTTPProvider(os.getenv("ARBITRUM_RPC_URL", ARBITRUM_RPC)))
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
    """Retrospective concentration from current indexed debt balances."""
    positions = api.get_market_positions(market_id, borrowers_only=True)
    return _concentration(int(p["state"]["borrowAssets"] or 0) for p in positions)


def _concentration(exposures):
    debts = sorted((x for x in exposures if x > 0), reverse=True)
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


def historical_concentration(market_id, block, timestamp):
    """Discover historical Borrow recipients and reconcile all shares at one block."""
    query = """
    query($id: String!, $end: Int!, $first: Int!, $skip: Int!) {
      marketTransactions(first: $first, skip: $skip, where: {
        chainId_in:[42161], marketUniqueKey_in:[$id], type_in:[Borrow], timestamp_lte:$end
      }) {
        items { blockNumber user { address } }
        pageInfo { countTotal }
      }
    }
    """
    mid = bytes.fromhex(market_id[2:])
    _, _, _, market_shares, _, _ = arb_call(config.MORPHO_BLUE, "market", [mid], block)
    events = api._paginate(query, {"id": market_id, "end": timestamp}, "marketTransactions")
    users = {Web3.to_checksum_address(event["user"]["address"])
             for event in events if event["blockNumber"] <= block}
    shares = [arb_call(config.MORPHO_BLUE, "position", [mid, user], block)[1] for user in sorted(users)]
    if sum(shares) != market_shares:
        raise ValueError("Historical positions do not reconcile to market borrow shares")
    return {**_concentration(shares), "borrow_shares_reconciled": str(market_shares)}


def case_study():
    block = find_block_by_timestamp(PRE_ANNOUNCEMENT_TIMESTAMP)
    timestamp = arb_w3().eth.get_block(block)["timestamp"]
    try:
        concentration = historical_concentration(XUSD_MARKET_ID, block, timestamp)
    except (Web3RPCError, RequestException, ValueError):
        return {"evidence": "retrospective_only", "reference_block": block,
                "historical_concentration": None, "historical_oracle_class": None,
                "retrospective_concentration": concentration_metrics(XUSD_MARKET_ID),
                "retrospective_debt": realized_bad_debt(XUSD_MARKET_ID)}
    oracle_address = "0x1837efFC34Bb5a96EFdA00d53560799bE3a4226E"
    return {"evidence": "historical_concentration", "reference_block": block,
            "historical_concentration": concentration,
            "historical_oracle_class": classify_oracle_arbitrum(oracle_address, block)["class"],
            "retrospective_debt": realized_bad_debt(XUSD_MARKET_ID)}


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
