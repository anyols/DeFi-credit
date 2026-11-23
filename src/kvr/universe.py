
import pandas as pd
from eth_abi import encode as abi_encode
from eth_utils import keccak

import config
from . import api, chain, oracle

_MARKET_PARAMS_TYPES = ["address", "address", "address", "address", "uint256"]


def implied_collateral_price_usd(oracle_addr, collateral_decimals, loan_decimals, block):
    """None if the oracle reverts or the implied price falls outside config.PLAUSIBLE_PRICE_USD
    (loan token is always USDC in this universe, so no price conversion is needed on the supply
    side at all -- this check is purely about whether the market's own collateral valuation, and
    therefore its LTV math, can be trusted; see config.py for what this caught)."""
    try:
        price = oracle.read_price(oracle_addr, block)
    except Exception:
        return None
    implied = price * (10 ** (collateral_decimals - loan_decimals)) / 1e36
    lo, hi = config.PLAUSIBLE_PRICE_USD
    return implied if lo <= implied <= hi else None


def verify_market_id(market_id_hex, loan_token, collateral_token, oracle, irm, lltv):
    packed = abi_encode(_MARKET_PARAMS_TYPES, [loan_token, collateral_token, oracle, irm, int(lltv)])
    computed = "0x" + keccak(packed).hex()
    return computed.lower() == market_id_hex.lower()


def candidate_market_ids(min_supply_usd=None, pool_size=None, chain_id=None):
    """Top `pool_size` Ethereum USDC markets by supply, with nonzero borrows, from the Morpho
    API's current state. Candidate selection only -- not the authoritative values used
    downstream, and not yet filtered for a usable oracle (see select_universe)."""
    min_supply_usd = min_supply_usd if min_supply_usd is not None else config.MIN_SUPPLY_USD
    pool_size = pool_size or config.TOP_N_MARKETS * 3
    chain_id = chain_id or config.CHAIN_ID
    q = """
    query($loan: [String!], $chainIds: [Int!], $minSupply: Float, $first: Int) {
      markets(first: $first, orderBy: SupplyAssetsUsd, orderDirection: Desc,
              where: { loanAssetAddress_in: $loan, chainId_in: $chainIds,
                       supplyAssetsUsd_gte: $minSupply, borrowAssetsUsd_gte: 1 }) {
        items { marketId }
      }
    }
    """
    data = api.graphql(q, {"loan": [config.USDC_ETHEREUM], "chainIds": [chain_id], "minSupply": min_supply_usd, "first": pool_size})
    return [m["marketId"] for m in data["markets"]["items"]]


def select_universe(top_n=None, block=None, pool_size=None):
    """Pulls a wider candidate pool (by default 3x top_n), drops any market whose oracle
    reverts or implies an implausible collateral price, and returns the top_n that remain,
    still ranked by supply. Returns (market_ids, excluded) where excluded is a list of
    {market_id, collateral_symbol, reason} for every market dropped -- keep this list, it's
    itself a finding worth reporting, not just a filtering step.
    """
    top_n = top_n or config.TOP_N_MARKETS
    block = block or config.SNAPSHOT_BLOCK
    pool = candidate_market_ids(pool_size=pool_size)

    kept, excluded = [], []
    for market_id in pool:
        if len(kept) >= top_n:
            break
        full = api.get_market(market_id)
        symbol = full["collateralAsset"]["symbol"] if full.get("collateralAsset") else "IDLE"
        if not full.get("oracle"):
            kept.append(market_id)
            continue
        implied = implied_collateral_price_usd(
            full["oracle"]["address"], full["collateralAsset"]["decimals"], full["loanAsset"]["decimals"], block
        )
        if implied is None:
            excluded.append({"market_id": market_id, "collateral_symbol": symbol, "reason": "oracle reverts or implies an implausible price"})
            continue
        kept.append(market_id)
    return kept, excluded


def build_markets(market_ids=None, block=None):
    """Returns a DataFrame: one row per market in the universe, with MarketParams (read on-chain,
    pinned to `block`) and an id-verification flag.
    """
    block = block or config.SNAPSHOT_BLOCK
    if market_ids is None:
        market_ids, _excluded = select_universe(block=block)

    rows = []
    for market_id in market_ids:
        full = api.get_market(market_id)
        loan_token = full["loanAsset"]["address"]
        collateral_token = full["collateralAsset"]["address"] if full.get("collateralAsset") else "0x" + "00" * 20
        oracle = full["oracle"]["address"] if full.get("oracle") else "0x" + "00" * 20
        irm = full["irmAddress"]
        lltv = int(full["lltv"])

        id_ok_offchain = verify_market_id(market_id, loan_token, collateral_token, oracle, irm, lltv)
        onchain_params = chain.call_morpho("idToMarketParams", [bytes.fromhex(market_id[2:])], block)
        id_ok_onchain = onchain_params is not None and tuple(
            a.lower() if isinstance(a, str) else a for a in onchain_params
        ) == tuple(a.lower() if isinstance(a, str) else a for a in (loan_token, collateral_token, oracle, irm, lltv))

        rows.append(
            {
                "market_id": market_id,
                "collateral_symbol": full["collateralAsset"]["symbol"] if full.get("collateralAsset") else None,
                "loan_token": loan_token,
                "collateral_token": collateral_token,
                "oracle": oracle,
                "irm": irm,
                "lltv": lltv,
                "api_supply_usd": full["state"]["supplyAssetsUsd"] if full.get("state") else None,
                "api_borrow_usd": full["state"]["borrowAssetsUsd"] if full.get("state") else None,
                "api_utilization": full["state"]["utilization"] if full.get("state") else None,
                "id_verified_offchain": id_ok_offchain,
                "id_verified_onchain": id_ok_onchain,
            }
        )
    return pd.DataFrame(rows)


def save(df, path="data/processed/markets.parquet"):
    df.to_parquet(path)
    return path
