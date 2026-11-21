"""T1 (SPEC.md section 5): sum of reconstructed per-position borrow assets vs the market's own
accrued totalBorrowAssets, rel err < 1e-6. Needs a live archive RPC (config.RPC_URL /
config.SNAPSHOT_BLOCK) and Arnau's hand-written kvr/morpho_math.py -- skips cleanly without either.
"""

import importlib

import pytest

import config
from kvr import chain, positions, universe

pytestmark = pytest.mark.skipif(
    not config.RPC_URL or not config.SNAPSHOT_BLOCK,
    reason="RPC_URL/SNAPSHOT_BLOCK not set; see .env.example",
)


def _morpho_math():
    try:
        return importlib.import_module("kvr.morpho_math")
    except ModuleNotFoundError:
        pytest.skip("kvr/morpho_math.py not written yet (Arnau's file, see SPEC.md section 1.1)")


def test_t1_borrow_assets_reconcile():
    mm = _morpho_math()
    markets = universe.build_markets()
    block = config.SNAPSHOT_BLOCK
    block_ts = chain.w3().eth.get_block(block)["timestamp"]

    for market_id in markets["market_id"]:
        raw_market = positions.fetch_market_state(market_id, block)
        _supply_assets, accrued_borrow_assets, _supply_shares, accrued_borrow_shares = positions.accrued_market_totals(
            market_id, raw_market, block_ts
        )
        pos = positions.build_positions(market_id, block)
        reconstructed = sum(
            mm.toAssetsUp(int(r.borrow_shares), accrued_borrow_assets, accrued_borrow_shares)
            for r in pos.itertuples()
            if r.borrow_shares > 0
        )
        rel_err = abs(reconstructed - accrued_borrow_assets) / max(accrued_borrow_assets, 1)
        assert rel_err < 1e-6, f"{market_id}: reconstructed={reconstructed} accrued={accrued_borrow_assets}"
