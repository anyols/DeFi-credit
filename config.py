import os
from pathlib import Path


def _load_dotenv(path=".env"):
    path = Path(__file__).parent / path
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

CHAIN_ID = 1  # Ethereum mainnet

RPC_URL = os.environ.get("RPC_URL", "")

# Pinned once an archive RPC was connected (2026-11-21), then frozen for the rest of the
# project so every on-chain read is reproducible. 10 blocks behind mainnet head at pin time.
SNAPSHOT_BLOCK = 26465719

# Verified against docs.morpho.org/get-started/resources/addresses and cross-checked live via
# the Morpho API (market.morphoBlue.address) for every market in the universe below.
MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

MORPHO_GRAPHQL_URL = "https://api.morpho.org/graphql"
MORPHO_REST_BASE = "https://api.morpho.org/v0"
USDC_ETHEREUM = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

# Universe (scope change: no vault). Ethereum Morpho Blue markets with loanToken=USDC,
# totalSupplyAssets >= MIN_SUPPLY_USD, and nonzero borrows, top TOP_N_MARKETS by supply.
# Candidate selection uses the Morpho API's current state (43 markets meet the $5M+/nonzero-
# borrow bar as of this writing; membership doesn't change within the ~1 day since
# SNAPSHOT_BLOCK was pinned); every number used downstream is a fresh on-chain read pinned to
# SNAPSHOT_BLOCK, not the API's current state. 15 rather than 20: Task 4's per-event
# reconstruction (hourly oracle sampling over 72h per liquidation, across up to 24 months) scales
# with market count, and 15 keeps that tractable.
MIN_SUPPLY_USD = 5_000_000
TOP_N_MARKETS = 15

# Plausibility bounds on an oracle's implied collateral-in-USD price, used to drop markets with
# a broken oracle from the universe (universe.select_universe). Found by hand: a PAXG market
# implying $4.5 quadrillion/PAXG, a WETH market implying $4.7 trillion/ETH, and two "msY"
# markets whose price() call reverts outright -- all four would otherwise have ranked in the
# top 10 by supply. Deliberately wide (not a per-asset range): it only needs to catch "wrong by
# orders of magnitude", not flag genuinely volatile but real prices.
PLAUSIBLE_PRICE_USD = (1e-6, 1e7)

# M7 scenario grid (see SPEC.md section 4, M7). Exact per-asset buckets are refined in scenarios.py.
UNDERLYING_SHOCK_RANGE = (-0.60, -0.10, 0.05)  # (min, max, step) on the loan/collateral price ratio
DEPEG_SHOCK_RANGE = (-0.80, -0.02, 0.02)  # for LST/LRT/yield-stable collateral, depeg-only
DEPTH_STRESS_KAPPA = (1, 2, 4)
PATH_STEPS = (1, 6)  # 1 = gap, 6 = gradual

# M9 EL: how many DefiLlama 500-day chunks to pull and stitch together for exceedance-frequency
# history (DefiLlama caps a single request at 500 points -- see scenarios._MAX_SPAN). Found by
# hand while fixing the EL==0 bug: even a correctly-declustered 7d horizon stayed ~0 on a single
# 500-day (~1.4yr) window, because that window simply contains no observed BTC crash large
# enough to overlap with the shock grid's lossy buckets (-25% or worse) -- not a horizon problem,
# a sample-length problem. 5 chunks reaches back ~6.8 years, into BTC's 2022 (FTX) and 2021-2022
# drawdowns, without needing an aggregator API key.
PRICE_HISTORY_CHUNKS = 5
