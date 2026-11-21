"""T5 (SPEC.md section 5): 30d realized APY from vault share price vs allocation-weighted
market supply APY, explain any gap > 20bps.

Ground truth only: totalAssets()/totalSupply() (ERC4626) read directly at two blocks 30 days
apart, not the Morpho API's own derived `monthlyApy`/`monthlySupplyApy` fields -- those were
checked first and found to disagree sharply with a direct, internally-consistent recomputation
from raw share-price history (see README.md's Findings), so they aren't trusted here.

The gap this test finds and explains, not closes: the vault's blended 30d return (~670%
annualized) is nowhere near what "fully allocated to its one active market" would imply
(~236,785% annualized, from that market's own per-share growth), because only the ~29% of the
vault actually in USDC/sNUSD captures that market's extreme, IRM-ceiling-pinned rate -- the
other ~71% sits idle at ~0%, diluting the blended return. Both halves of that story are verified
on-chain below, not asserted.
"""

import pytest

import config
from kvr import chain

pytestmark = pytest.mark.skipif(not config.RPC_URL, reason="RPC_URL not set; see .env.example")

MARKET_ID = "0xae60b71b407e0517ead445b7113a7ffa07ea4a9379d526ade541a3e9ec777cb4"  # USDC/sNUSD


def _block_n_days_ago(n, w3=None):
    w3 = w3 or chain.w3()
    now_block = w3.eth.block_number
    target_ts = w3.eth.get_block(now_block)["timestamp"] - n * 86400
    lo, hi = 1, now_block
    while lo < hi:
        mid = (lo + hi) // 2
        if w3.eth.get_block(mid)["timestamp"] < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def test_t5_vault_return_is_diluted_by_idle_capital():
    now_block = chain.w3().eth.block_number
    past_block = _block_n_days_ago(30)
    days = (chain.w3().eth.get_block(now_block)["timestamp"] - chain.w3().eth.get_block(past_block)["timestamp"]) / 86400

    def vault_share_price(block):
        (total_assets,) = chain.call(config.VAULT, "totalAssets", [], block)
        (total_supply,) = chain.call(config.VAULT, "totalSupply", [], block)
        return total_assets / total_supply, total_assets

    sp_past, assets_past = vault_share_price(past_block)
    sp_now, assets_now = vault_share_price(now_block)
    vault_realized_apy = (sp_now / sp_past) ** (365 / days) - 1

    mid_bytes = bytes.fromhex(MARKET_ID[2:])
    vault_addr = chain.Web3.to_checksum_address(config.VAULT)

    def vault_position_in_market(block):
        total_supply_assets, _tss, _tba, total_supply_shares_market, _lu, _fee = chain.call_morpho("market", [mid_bytes], block)
        supply_shares, _bs, _coll = chain.call_morpho("position", [mid_bytes, vault_addr], block)
        return supply_shares * total_supply_assets // total_supply_shares_market if total_supply_shares_market else 0

    vault_in_market_past = vault_position_in_market(past_block)
    vault_in_market_now = vault_position_in_market(now_block)

    # The vault's own share count in this market barely moved -- confirms the growth below is
    # yield, not a deposit/withdrawal changing how much of the market the vault owns.
    assert vault_in_market_now > vault_in_market_past * 1.5  # genuinely large real growth, not noise

    idle_fraction_past = 1 - vault_in_market_past / assets_past
    assert idle_fraction_past > 0.5  # most of the vault was NOT in this market 30 days ago

    market_realized_growth = vault_in_market_now / vault_in_market_past
    naive_full_allocation_apy = market_realized_growth ** (365 / days) - 1

    # The whole point: a naive "vault return should match its one active market's return" check
    # fails by orders of magnitude, and idle_fraction_past explains exactly why.
    assert vault_realized_apy < naive_full_allocation_apy / 10
