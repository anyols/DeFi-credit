

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
    """Raw on-chain Position structs for every address, via one multicall batch.

    supply_shares/borrow_shares/collateral are uint256/uint128 and can exceed int64 (seen in
    practice -- an ordinary ~1,133-token position in an 18-decimal collateral asset is already
    ~1.13e21 raw units, past int64's ~9.2e18 ceiling), so they're stored as decimal strings to
    stay integer-exact; parse back with int() before use.
    """
    id_bytes = bytes.fromhex(market_id[2:])
    calls = [("position", [id_bytes, chain.Web3.to_checksum_address(a)]) for a in addresses]
    results = chain.multicall_morpho(calls, block)
    rows = []
    for addr, r in zip(addresses, results):
        if r is None:
            continue
        supply_shares, borrow_shares, collateral = r
        rows.append(
            {
                "user": addr,
                "supply_shares": str(supply_shares),
                "borrow_shares": str(borrow_shares),
                "collateral": str(collateral),
            }
        )
    return pd.DataFrame(rows)


def w_taylor_compounded(x, n):
    """3-term Taylor expansion of e^(nx) - 1, integer-exact (MathLib.sol::wTaylorCompounded)."""
    first_term = x * n
    second_term = (first_term * first_term) // (2 * WAD)
    third_term = (second_term * first_term) // (3 * WAD)
    return first_term + second_term + third_term


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def accrued_market_totals(market_id, raw_market, snapshot_timestamp):
    """Apply Morpho's exact accrual formula. Returns (total_supply_assets, total_borrow_assets,
    total_supply_shares, total_borrow_shares) accrued to snapshot_timestamp.

    Matches Morpho.sol::_accrueInterest exactly, including its `if (marketParams.irm !=
    address(0))` guard -- Morpho's "idle market" (loanToken set, everything else zero, used only
    for uncollateralized parking) has no IRM and therefore no borrowing or accrual at all.
    """
    total_supply_assets, total_supply_shares, total_borrow_assets, total_borrow_shares, last_update, fee = raw_market
    elapsed = snapshot_timestamp - last_update
    if elapsed <= 0:
        return total_supply_assets, total_borrow_assets, total_supply_shares, total_borrow_shares

    params = chain.call_morpho("idToMarketParams", [bytes.fromhex(market_id[2:])], config.SNAPSHOT_BLOCK)
    loan_token, collateral_token, oracle, irm, lltv = params
    if irm.lower() == ZERO_ADDRESS:
        return total_supply_assets, total_borrow_assets, total_supply_shares, total_borrow_shares

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
