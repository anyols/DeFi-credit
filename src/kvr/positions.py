"""M2: borrower discovery + position state at SNAPSHOT_BLOCK, with interest accrued to the
snapshot timestamp.

Borrower discovery uses the Morpho API's indexed `marketPositions` instead of scanning
`Borrow`/`SupplyCollateral` logs: a free-tier Ethereum RPC key (e.g. Alchemy) caps eth_getLogs at
10 blocks per call, so scanning a market's full history (often >1M blocks) via getLogs is not
practical without a paid plan. The API gives the same set of addresses already indexed; every
number used for the actual math (shares, collateral, market totals) still comes from a raw
eth_call via chain.py at the exact pinned SNAPSHOT_BLOCK, not from the API.

Accrual replicates Morpho.sol::_accrueInterest and MathLib.sol::wTaylorCompounded exactly
(verified against source, both MIT/GPL-2.0, morpho-org/morpho-blue):
    elapsed  = snapshot_timestamp - lastUpdate
    rate     = borrowRateView(marketParams, market)        # per-second, WAD
    interest = totalBorrowAssets * wTaylorCompounded(rate, elapsed) / WAD
    totalBorrowAssets += interest; totalSupplyAssets += interest
This accrual step is plumbing (Morpho's generic 3-term-Taylor compounding, used identically by
every market regardless of IRM), not the AdaptiveCurveIRM curve/drift model -- that is Arnau's
irm.py (M4), used for forward-looking rate projections, not for accruing past interest here.
"""

import pandas as pd

import config
from . import api, chain

WAD = 10**18


def discover_borrowers(market_id):
    positions = api.get_market_positions(market_id, borrowers_only=True)
    return [p["user"]["address"] for p in positions]


def discover_all_participants(market_id):
    positions = api.get_market_positions(market_id, borrowers_only=False)
    return [p["user"]["address"] for p in positions]


def fetch_market_state(market_id, block):
    """Raw on-chain Market struct at `block`: (totalSupplyAssets, totalSupplyShares,
    totalBorrowAssets, totalBorrowShares, lastUpdate, fee)."""
    result = chain.call_morpho("market", [bytes.fromhex(market_id[2:])], block)
    if result is None:
        raise RuntimeError(f"market({market_id}) reverted at block {block}")
    return result


def fetch_positions(market_id, addresses, block):
    """Raw on-chain Position structs for every address, via one multicall batch."""
    id_bytes = bytes.fromhex(market_id[2:])
    calls = [("position", [id_bytes, chain.Web3.to_checksum_address(a)]) for a in addresses]
    results = chain.multicall_morpho(calls, block)
    rows = []
    for addr, r in zip(addresses, results):
        if r is None:
            continue
        supply_shares, borrow_shares, collateral = r
        rows.append(
            {"user": addr, "supply_shares": supply_shares, "borrow_shares": borrow_shares, "collateral": collateral}
        )
    return pd.DataFrame(rows)


def w_taylor_compounded(x, n):
    """3-term Taylor expansion of e^(nx) - 1, integer-exact (MathLib.sol::wTaylorCompounded)."""
    first_term = x * n
    second_term = (first_term * first_term) // (2 * WAD)
    third_term = (second_term * first_term) // (3 * WAD)
    return first_term + second_term + third_term


def accrued_market_totals(market_id, raw_market, snapshot_timestamp):
    """Apply Morpho's exact accrual formula. Returns (total_supply_assets, total_borrow_assets,
    total_supply_shares, total_borrow_shares) accrued to snapshot_timestamp.
    """
    total_supply_assets, total_supply_shares, total_borrow_assets, total_borrow_shares, last_update, fee = raw_market
    elapsed = snapshot_timestamp - last_update
    if elapsed <= 0:
        return total_supply_assets, total_borrow_assets, total_supply_shares, total_borrow_shares

    params = chain.call_morpho("idToMarketParams", [bytes.fromhex(market_id[2:])], config.SNAPSHOT_BLOCK)
    loan_token, collateral_token, oracle, irm, lltv = params
    market_params_tuple = (loan_token, collateral_token, oracle, irm, lltv)
    (rate,) = chain.call(irm, "borrowRateView", [market_params_tuple, raw_market], config.SNAPSHOT_BLOCK)

    interest = total_borrow_assets * w_taylor_compounded(rate, elapsed) // WAD
    return total_supply_assets + interest, total_borrow_assets + interest, total_supply_shares, total_borrow_shares


def build_positions(market_id, block=None):
    block = block or config.SNAPSHOT_BLOCK
    addresses = discover_all_participants(market_id)
    return fetch_positions(market_id, addresses, block)


def save(df, market_id, path_template="data/processed/positions_{}.parquet"):
    path = path_template.format(market_id[2:10])
    df.to_parquet(path)
    return path
