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

# Zombie market classifier (spread.classify_zombie): utilization >= ZOMBIE_UTIL_THRESHOLD for
# >= ZOMBIE_MIN_DAYS_STUCK consecutive days, AND no Repay event in the last
# ZOMBIE_MIN_DAYS_SINCE_REPAY days. A market like this has supply that is, in practice, not
# withdrawable and not actively being managed by its borrowers -- its on-chain "spread" is a
# continuous-compounding artifact (see irm.apy_from_per_second_rate), not a real, earnable yield.
ZOMBIE_UTIL_THRESHOLD = 0.99
ZOMBIE_MIN_DAYS_STUCK = 30
ZOMBIE_MIN_DAYS_SINCE_REPAY = 30

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

# Task 4 (liq_edge.py): "when do liquidators walk away, and who gets paid". 24 months, named
# rather than hardcoded. The cheap per-event metrics (gas cost, DEX-vs-MM profitability,
# insolvency, left-on-the-table) run over every event in the window; only the EXPENSIVE
# per-event reconstruction (hourly oracle sampling to find when a position first breached,
# below) is capped per market -- found by hand that the full 24-month universe has 886 real
# Liquidate events, and reconstructing all of them at ~15 oracle reads each (see
# LIQ_EDGE_MAX_DELAY_HOURS) would take well over an hour of RPC round trips for a number this
# project already gets from a bounded, labeled sample -- the same sampling precedent as T4's own
# "replay the last 50 liquidations", not an exhaustive claim.
LIQ_EDGE_LOOKBACK_DAYS = 730
LIQ_EDGE_DELAY_SAMPLE_PER_MARKET = 30
# How far back (in hours) the delay-to-liquidation search looks for the oracle price first
# crossing a position's breakeven price, via exponential-backoff + binary refine (not a linear
# hourly scan -- O(log) reads, not O(hours)). Tried first at the cheap default; an event that
# hits this bound (the breach predates it) is retried once at LIQ_EDGE_MAX_DELAY_HOURS_EXTENDED
# (review fix 4) rather than reported as a fabricated number past what was actually searched.
LIQ_EDGE_MAX_DELAY_HOURS = 168
LIQ_EDGE_MAX_DELAY_HOURS_EXTENDED = 24 * LIQ_EDGE_LOOKBACK_DAYS  # the full lookback window, only
# tried when the cheap 168h search hits its own bound -- the common case (most real liquidations
# are fast) never pays for this, only the AZND/RLP/USR/wstUSR-style "stuck for a long time" cases.
LIQ_EDGE_BLOCKS_PER_HOUR = 300  # ~12s/block on Ethereum mainnet -- an approximation, labeled

# Review fix 1: events below this repaid-USD size are dust -- excluded from the headline
# profitability stats (gas cost dominates for a tiny liquidation almost by construction) and
# reported as a separate count/volume instead of silently diluting the real picture.
LIQ_EDGE_DUST_THRESHOLD_USD = 1_000

# Review fix 2: per-collateral-class exit model, replacing the old one-size-fits-all "MM" spread.
# BTC/ETH family (spread.classify_collateral in BTC/ETH-LST): a liquidator hedges the seized
# collateral via a liquid perpetual future (BTC-PERP/ETH-PERP are among the deepest crypto perp
# markets) rather than selling spot -- cost modeled as a flat spread (taker fee + a short-horizon
# funding assumption), since no real perp-cost feed is integrated. An assumption, not a measurement.
LIQ_EDGE_HEDGE_COST_BPS = 5

# Redeemable yield-bearing collateral: exit cost = cooldown_days * cost_of_capital (daily) +
# redemption_haircut_bps, instead of a DEX/MM sale. Verified by web research (2026-11-22; see
# the Task 4 review report) that NONE of these had a single, reliably-sourced cooldown/fee
# number: RLP and USR (both Resolv Protocol) gate redemption on a 110% collateralization ratio,
# not a fixed time window. wstUSR: confirmed via multiple independent reputable sources
# (Chainalysis's own incident blog, Rekt.news, The Block, Cointelegraph) that Resolv suffered a
# real $23-25M exploit on 2026-03-22 (a compromised signing key, not a contract flaw); the
# attacker minted unbacked USR, converted a large share to wstUSR, and used THAT wstUSR as
# collateral on a Morpho market to extract funds. Resolv's own public position is that the
# collateral pool remains fully backed and no user assets were lost, and losses were capped via
# a later contract upgrade -- but wstUSR's redemption/exit mechanics for an ordinary holder in
# that period are not independently verified here, which is the actual reason its cost stays
# unestimated, not a blanket "hacked, do not use" (that earlier phrasing overstated a secondary
# source and has been corrected). apyUSD (Apyx) has a real but unquantified on-chain unlock
# delay. Only fxSAVE (f(x) Protocol)'s own primary-source integration docs describe something
# close to synchronous, cooldown-free redemption, used here with low confidence, not as a
# settled fact. None means "not estimable" -- never a guessed number (this project's own rule)
# -- so RLP/USR/wstUSR/apyUSD's exit cost is reported as not estimable rather than computed from
# an invented day count.
LIQ_EDGE_REDEMPTION_COOLDOWN_DAYS = {"RLP": None, "USR": None, "wstUSR": None, "apyUSD": None, "fxSAVE": 0}
LIQ_EDGE_REDEMPTION_HAIRCUT_BPS = {"RLP": None, "USR": None, "wstUSR": None, "apyUSD": None, "fxSAVE": 0}

# Opportunity cost of capital for both the redemption-cooldown and PT-hold-to-maturity carry
# calculations. Prefer the live Aave USDC 90d supply rate (spread.aave_usdc_supply_rate_90d_avg)
# as a real, already-used-elsewhere risk-free proxy; this is the fallback if that call fails.
LIQ_EDGE_COST_OF_CAPITAL_ANNUAL_FALLBACK = 0.05

# Max allowed deviation (in days) between a return window's actual elapsed time and its nominal
# horizon_days, before that window is dropped from exceedance_frequency rather than silently
# treated as a clean N-day return. Found by hand while checking whether 2020-03-12 was captured
# correctly: the merged 6.8-year history has exactly one real gap (2022-09-18 missing, so
# 2022-09-17->2022-09-19 is 2 days of real elapsed time, not 1) that would otherwise have been
# computed as a plain 1-day return. 0.5 days catches that gap (and DefiLlama's own per-day
# timestamp jitter, generally well under an hour, passes easily).
PRICE_GAP_TOLERANCE_DAYS = 0.5
