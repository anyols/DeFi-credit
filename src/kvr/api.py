"""Morpho API client (GraphQL). The REST v0 API in SPEC.md is gone (404 as of this writing);
GraphQL at api.morpho.org/graphql is the only live endpoint, confirmed by schema introspection.
"""

import time

import requests

import config

_PAGE_SIZE = 500


def graphql(query, variables=None, max_retries=4):
    body = {"query": query, "variables": variables or {}}
    for attempt in range(max_retries):
        r = requests.post(config.MORPHO_GRAPHQL_URL, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(min(2**attempt, 20))
            continue
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"Morpho API error: {data['errors']}")
        return data["data"]
    raise RuntimeError("Morpho API: exhausted retries on 429")


def _paginate(query, variables, path):
    """path: dotted key into the response pointing at a {items, pageInfo{countTotal}} object."""
    items = []
    skip = 0
    while True:
        vars_page = {**variables, "first": _PAGE_SIZE, "skip": skip}
        data = graphql(query, vars_page)
        for key in path.split("."):
            data = data[key]
        items.extend(data["items"])
        skip += len(data["items"])
        if skip >= data["pageInfo"]["countTotal"] or not data["items"]:
            break
    return items


def search_vaults(search, chain_id_in=None):
    """Vault summaries matching a name/curator search string, e.g. 'gauntlet'."""
    query = """
    query($search: String, $chainIds: [Int!], $first: Int, $skip: Int) {
      vaults(first: $first, skip: $skip, where: { search: $search, chainId_in: $chainIds }) {
        items {
          address name symbol listed
          chain { id network }
          state { totalAssets totalAssetsUsd apy netApy fee curator }
        }
        pageInfo { countTotal }
      }
    }
    """
    return _paginate(query, {"search": search, "chainIds": chain_id_in}, "vaults")


def get_vault_allocation(vault_address):
    """Vault state plus its per-market allocation (caps and supplied assets)."""
    query = """
    query($address: String!) {
      vaults(first: 1, where: { address_in: [$address] }) {
        items {
          address name symbol listed
          state {
            totalAssets totalAssetsUsd apy netApy fee
            allocation {
              market {
                marketId lltv
                loanAsset { symbol }
                collateralAsset { symbol }
                morphoBlue { address chain { id } }
              }
              supplyAssets supplyAssetsUsd supplyCapUsd
            }
          }
        }
      }
    }
    """
    data = graphql(query, {"address": vault_address.lower()})
    items = data["vaults"]["items"]
    if not items:
        raise ValueError(f"no vault found for address {vault_address}")
    return items[0]


def get_market(market_id, chain_id=None):
    query = """
    query($id: String!, $chainId: Int!) {
      marketById(marketId: $id, chainId: $chainId) {
        marketId lltv creationBlockNumber
        loanAsset { address symbol decimals }
        collateralAsset { address symbol decimals }
        irmAddress
        oracle { address }
        morphoBlue { address chain { id } }
        state { supplyShares borrowShares utilization price rateAtTarget liquidityAssets supplyAssetsUsd borrowAssetsUsd }
      }
    }
    """
    data = graphql(query, {"id": market_id, "chainId": chain_id or config.CHAIN_ID})
    return data["marketById"]


def get_market_positions(market_unique_key, borrowers_only=False):
    """All positions in a market. borrowers_only=True restricts to borrowShares_gte: 1."""
    where_extra = ", borrowShares_gte: \"1\"" if borrowers_only else ""
    query = f"""
    query($id: String!, $first: Int, $skip: Int) {{
      marketPositions(first: $first, skip: $skip,
                       where: {{ marketUniqueKey_in: [$id]{where_extra} }}) {{
        items {{
          healthFactor
          user {{ address }}
          state {{ supplyShares borrowShares collateral borrowAssets supplyAssets collateralUsd }}
        }}
        pageInfo {{ countTotal }}
      }}
    }}
    """
    return _paginate(query, {"id": market_unique_key}, "marketPositions")


def get_market_liquidations(market_unique_key, limit=None, before_timestamp=None, since_timestamp=None):
    """before_timestamp pins the result to "the last `limit` liquidations as of this timestamp"
    instead of "as of whenever this happens to be called" -- found necessary while re-running
    T4 against an actively-liquidated market (USDC/WBTC): without it, re-running minutes apart
    returns a different window as new real liquidations occur, which is not reproducible and
    not consistent with this project's SNAPSHOT_BLOCK pinning everywhere else. Pass
    config.SNAPSHOT_BLOCK's own timestamp for a reproducible call.

    since_timestamp (Task 4: liq_edge.py's 24-month lookback) adds a lower bound; also includes
    each transaction's user (the liquidated borrower, not the liquidator in `data`) and
    repaidShares/badDebtShares, needed to reconstruct a position's state before liquidation.
    """
    # Each optional variable is only declared/passed when used -- GraphQL's NoUnusedVariables
    # validation rejects a declared-but-unreferenced variable, even a nullable one passed as None.
    var_decls, wheres, variables = [], [], {}
    if before_timestamp is not None:
        var_decls.append("$beforeTs: Int")
        wheres.append("timestamp_lte: $beforeTs")
        variables["beforeTs"] = before_timestamp
    if since_timestamp is not None:
        var_decls.append("$sinceTs: Int")
        wheres.append("timestamp_gte: $sinceTs")
        variables["sinceTs"] = since_timestamp
    var_decl = ("".join(d + ", " for d in var_decls))
    where = ("".join(", " + w for w in wheres))
    query = f"""
    query($id: String!, {var_decl} $first: Int, $skip: Int) {{
      marketTransactions(first: $first, skip: $skip,
                          where: {{ marketUniqueKey_in: [$id], type_in: [Liquidation]{where} }}) {{
        items {{
          txHash timestamp blockNumber
          user {{ address }}
          data {{
            ... on MarketTransactionLiquidationData {{
              liquidator repaidAssets repaidShares seizedAssets badDebtAssets badDebtShares
            }}
          }}
        }}
        pageInfo {{ countTotal }}
      }}
    }}
    """
    items = _paginate(query, {"id": market_unique_key, **variables}, "marketTransactions")
    items.sort(key=lambda t: t["timestamp"], reverse=True)
    return items[:limit] if limit else items


def get_last_transaction(market_unique_key, tx_type, before_timestamp=None):
    """Most recent marketTransaction of `tx_type` (e.g. "Repay") for a market, as of
    before_timestamp (SNAPSHOT_BLOCK's own timestamp for reproducibility), or None if there has
    never been one. Used by stress.classify_zombie to find each zombie market's last Repay.
    """
    if before_timestamp is not None:
        var_decl, where, variables = "$beforeTs: Int,", ", timestamp_lte: $beforeTs", {"beforeTs": before_timestamp}
    else:
        var_decl, where, variables = "", "", {}
    query = f"""
    query($id: String!, {var_decl} $first: Int, $skip: Int) {{
      marketTransactions(first: $first, skip: $skip,
                          where: {{ marketUniqueKey_in: [$id], type_in: [{tx_type}]{where} }}) {{
        items {{ txHash timestamp blockNumber }}
        pageInfo {{ countTotal }}
      }}
    }}
    """
    items = _paginate(query, {"id": market_unique_key, **variables}, "marketTransactions")
    return max(items, key=lambda t: t["timestamp"]) if items else None


def get_market_transfers(market_unique_key, tx_type, before_timestamp=None, since_timestamp=None):
    """All Supply/Withdraw/Borrow/Repay transactions for a market (type_in: [tx_type]), with
    their assets/shares -- MarketTransactionTransferData, the same union member all four share
    (verified by schema introspection). Used by spread.zombie_principal to separate a zombie
    market's real lender principal from accrued interest. Most callers want
    get_market_transfers_chunked instead -- a sufficiently active market has more events than
    the API's own skip<=10000 pagination ceiling allows in one unbounded call (found by hand on
    RLP: over 10,000 Supply/Withdraw events).
    """
    var_decls, wheres, variables = [], [], {}
    if before_timestamp is not None:
        var_decls.append("$beforeTs: Int")
        wheres.append("timestamp_lte: $beforeTs")
        variables["beforeTs"] = before_timestamp
    if since_timestamp is not None:
        var_decls.append("$sinceTs: Int")
        wheres.append("timestamp_gte: $sinceTs")
        variables["sinceTs"] = since_timestamp
    var_decl = "".join(d + ", " for d in var_decls)
    where = "".join(", " + w for w in wheres)
    query = f"""
    query($id: String!, {var_decl} $first: Int, $skip: Int) {{
      marketTransactions(first: $first, skip: $skip,
                          where: {{ marketUniqueKey_in: [$id], type_in: [{tx_type}]{where} }}) {{
        items {{
          txHash timestamp blockNumber
          data {{ ... on MarketTransactionTransferData {{ assets shares }} }}
        }}
        pageInfo {{ countTotal }}
      }}
    }}
    """
    return _paginate(query, {"id": market_unique_key, **variables}, "marketTransactions")


def get_market_transfers_chunked(market_unique_key, tx_type, since_timestamp, before_timestamp):
    """Same as get_market_transfers, but bisects the [since_timestamp, before_timestamp] window
    on hitting the API's skip<=10000 pagination ceiling, so a market with more events than that
    ceiling allows (confirmed on RLP) still gets a complete, not truncated, result.
    """
    try:
        return get_market_transfers(market_unique_key, tx_type, before_timestamp=before_timestamp, since_timestamp=since_timestamp)
    except RuntimeError as e:
        if "skip must not be greater than" not in str(e):
            raise
        mid_ts = (since_timestamp + before_timestamp) // 2
        if mid_ts <= since_timestamp or mid_ts >= before_timestamp:
            raise  # window can't be split further (sub-second range) -- a genuine API limit, not a bug to paper over
        return get_market_transfers_chunked(market_unique_key, tx_type, since_timestamp, mid_ts) + get_market_transfers_chunked(
            market_unique_key, tx_type, mid_ts + 1, before_timestamp
        )
