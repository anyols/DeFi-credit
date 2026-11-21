"""M1: vault -> markets -> MarketParams, with on-chain id verification.

keccak256(abi.encode(MarketParams)) == marketId is Morpho Blue's own definition of a market id
(see MarketParamsLib.id in morpho-org/morpho-blue); this function checks that the API's reported
params for each market the vault allocates to actually hash to the id the API gave us, and that
idToMarketParams(id) on-chain agrees. Both must hold for the market to be trustworthy input.
"""

import pandas as pd
from eth_abi import encode as abi_encode
from eth_utils import keccak

import config
from . import api, chain

_MARKET_PARAMS_TYPES = ["address", "address", "address", "address", "uint256"]


def verify_market_id(market_id_hex, loan_token, collateral_token, oracle, irm, lltv):
    packed = abi_encode(_MARKET_PARAMS_TYPES, [loan_token, collateral_token, oracle, irm, int(lltv)])
    computed = "0x" + keccak(packed).hex()
    return computed.lower() == market_id_hex.lower()


def build_markets(vault_address=None, block=None):
    """Returns a DataFrame: one row per market the vault allocates to, with MarketParams,
    the vault's share of market supply, and an id-verification flag.
    """
    vault_address = vault_address or config.VAULT
    block = block or config.SNAPSHOT_BLOCK
    vault = api.get_vault_allocation(vault_address)

    rows = []
    for alloc in vault["state"]["allocation"]:
        m = alloc["market"]
        market_id = m["marketId"]
        oracle = m.get("oracle", {}).get("address") if m.get("oracle") else None
        full = api.get_market(market_id)
        loan_token = full["loanAsset"]["address"]
        collateral_token = full["collateralAsset"]["address"] if full.get("collateralAsset") else "0x" + "00" * 20
        oracle = full["oracle"]["address"] if full.get("oracle") else "0x" + "00" * 20
        irm = full["irmAddress"]
        lltv = int(m["lltv"])

        id_ok_offchain = verify_market_id(market_id, loan_token, collateral_token, oracle, irm, lltv)
        onchain_params = chain.call_morpho("idToMarketParams", [bytes.fromhex(market_id[2:])], block)
        id_ok_onchain = onchain_params is not None and tuple(
            a.lower() if isinstance(a, str) else a for a in onchain_params
        ) == tuple(a.lower() if isinstance(a, str) else a for a in (loan_token, collateral_token, oracle, irm, lltv))

        rows.append(
            {
                "market_id": market_id,
                "loan_token": loan_token,
                "collateral_token": collateral_token,
                "oracle": oracle,
                "irm": irm,
                "lltv": lltv,
                "supply_cap_usd": alloc["supplyCapUsd"],
                "vault_supply_assets": int(alloc["supplyAssets"]),
                "id_verified_offchain": id_ok_offchain,
                "id_verified_onchain": id_ok_onchain,
            }
        )
    df = pd.DataFrame(rows)
    df["vault_id"] = vault_address
    return df


def save(df, path="data/processed/markets.parquet"):
    df.to_parquet(path)
    return path
