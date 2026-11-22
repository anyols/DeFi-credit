# Where DeFi lending risk actually sits: not where prices move, but where nobody can sell

Snapshot: block 26,465,719 (Ethereum mainnet). Universe: top 15 USDC Morpho Blue markets by
supply (`config.TOP_N_MARKETS`, `config.MIN_SUPPLY_USD=$5M`). Liquidation data: last 24 months
(`config.LIQ_EDGE_LOOKBACK_DAYS=730`), 886 real `Liquidate` events across the universe.

## TL;DR

1. **Liquid collateral (WBTC/cbBTC/wstETH): 609 liquidations over 24 months, 97-99% of volume
   liquidated profitably** (WBTC 99.3%, cbBTC 97.4%, wstETH 99.6% -- `liq_edge.profitability_stats`,
   volume-weighted, dust excluded), **median delay < 1 hour.** Liquidation works here; no edge
   for anyone.
2. **Collateral with no DEX exit (RLP, USR, wstUSR, PT notes, AZND): 5 of the 15 largest USDC
   markets are zombies** (`spread.classify_zombie`: >=99% utilization for 30d+, no Repay in
   30d+), pinned at a measured ~800% APR (`spread.onchain_supply_apr`, read from the on-chain
   IRM, not assumed), holding **$1.22B of reported supply -- 62% of this universe's total**. The
   delay-to-liquidation figures this project first computed for these markets did not survive a
   direct health-factor check (see Findings) and are excluded here. What's verified: the RLP
   market crystallized **$1.18M of bad debt** from a position that was already ~38% underwater
   before anyone could act, because RLP has zero DEX depth at any size or fee tier (`dex.best_route`).
3. **The model flags these structurally in advance**, before any loss prints: oracle class, DEX
   depth, and borrower concentration are all computable from indexed state alone, no stress run
   needed -- the same two checks (oracle class + concentration) flagged Stream/xUSD before its
   Nov 2025 collapse (see Backtest). The edge in markets like RLP/USR belongs to whoever can exit
   through issuer redemption, not to whoever hedges best -- and this project could not verify that
   redemption is actually available on reasonable terms for most of them (see Limitations).

## Findings

**Liquid markets: liquidation works, and it isn't close (fig 7, fig 8).** WBTC (239 events),
cbBTC (148), wstETH (222) over 24 months. Profitable share by count (88-91%) is meaningfully
lower than by USD volume (97-99%) because the unprofitable events are small -- gas dominates a
tiny liquidation almost by construction, so dust (<$1k repaid, `config.LIQ_EDGE_DUST_THRESHOLD_USD`)
is reported separately rather than folded into the headline. Gas is priced at the exact execution
block via Chainlink's own ETH/USD feed (`liq_edge.eth_usd_price_at_block`), not a daily average.
Median delay to liquidation is 0 hours for all three -- liquidator bots act same-block, often in
the same block as the price-feed update that makes the position liquidatable in the first place
(confirmed directly: one real WBTC event's oracle price moved from $56,758 to $56,015 -- healthy
to liquidatable -- within its own execution block).

**Illiquid markets: the delay claim didn't survive validation.** The project's first pass
reported multi-hundred-day delays for AZND/RLP/USR/wstUSR/PT-RLP-9APR2026, found by searching the
oracle's historical price against a breakeven computed once from the position's state just
before liquidation. Spot-checking that assumption by sampling health factor directly at 10
evenly spaced blocks across each claimed window (`liq_edge.validate_delay_window`) found a
healthy (HF>=1) sample inside **every one** of the five windows -- the fixed-breakeven search is
invalid over a long window when the position's own collateral or debt changed partway through it.
Confirmed root cause for RLP: an intermediate partial liquidation 9.6 days before the final one
changed the position's collateral from 630,000 tokens to 1 raw unit, which the search never
accounted for. These five delay numbers are therefore marked unverified and dropped from this
memo, not reported with a caveat. What remains verified without that search: all five markets are
classified zombies, their measured on-chain borrow rate is pinned at ~800% APR
(`spread.onchain_supply_apr`), and none has any Uniswap V3 or aggregator depth at any size
(`dex.best_route`, `dex.aggregator_quote_curve` -- both exhausted, zero result).

**Zombie markets (fig 10):** 5 of 15 markets, $1.22B of this universe's $1.97B reported supply
(62%) sits at >=99% utilization with no Repay event in 30+ days. This capital is not withdrawable
in practice and is not being actively managed by its borrowers.

**RLP case study.** On 2026-05-25, a liquidation seized ~630,000 RLP and repaid $776,865.78 of
debt -- but the position was already ~38% underwater at that moment (collateral worth ~$810,925
against $1,313,813 of debt, both read on-chain at the block just before this event), because RLP
has no DEX depth at all and no liquidator could act earlier without a loss. The $536,947 residual
then kept accruing interest at the market's pinned rate (confirmed directly: debt grew from
$956,349 to $1,079,489 in the following 9.6 days alone) until a second liquidation, 9.6 days
later, seized the last raw unit of collateral and the protocol recognized the remaining shortfall
-- **$1,181,253.93** -- as `badDebtAssets`. Two liquidations, not one freak event; the second is a
formality that crystallizes a loss the first one's own economics already guaranteed.

**Why DEX depth and redemption terms matter more here than price risk.** `spread.classify_zombie`
and `dex.best_route` are both computable from current state with no simulation. Every zombie
market in this universe would have shown zero DEX depth and a pinned IRM rate well before its
debt grew large enough to matter.

## Backtest: Stream / xUSD (Nov 2025)

Unchanged from the prior pass (`stream_case.py`, Arbitrum, pre-announcement block 395,834,395
found by binary search). Oracle class: `UNKNOWN` -- none of MorphoChainlinkOracleV2's standard
fields resolve on this contract. Concentration: 2 borrowers, HHI ~= 1.0, top-1 share 99.9998%.
Of the market's $294.2M still borrowed, Morpho's own mark-to-market shortfall estimate is $288.1M
(~98%), but only one `Liquidate` event has ever fired (writing off $187.57) and `realizedBadDebt`
is still $0 -- the damage is sitting unrealized, not crystallized. Two cheap, static checks
(oracle class + concentration) would have flagged this market as extreme before any stress
scenario ran, using nothing but the API's own indexed state -- the same two checks this memo's
zombie/DEX-depth findings above are built from.

## Limitations

Every named assumption lives in `config.py`, with its reasoning in the comment next to it:
- `SNAPSHOT_BLOCK`, `TOP_N_MARKETS=15`, `MIN_SUPPLY_USD=$5M` -- universe selection; membership can
  shift day to day, every number used is a fresh on-chain read pinned to the snapshot.
- `PLAUSIBLE_PRICE_USD` -- the oracle-sanity filter that drops a market with a reverting or
  absurd implied price; also applied to the wider cross-section after a PAXG and an sdeUSD market
  slipped through it there with $35B/$25B reported supply (genuine on-chain state, inflated by
  Morpho's own Taylor-series interest accrual over 400+ untouched days on a near-ceiling rate).
- `ZOMBIE_UTIL_THRESHOLD/ZOMBIE_MIN_DAYS_STUCK/ZOMBIE_MIN_DAYS_SINCE_REPAY` -- the zombie
  classifier's own thresholds (99% utilization, 30 days, 30 days).
- `UNDERLYING_SHOCK_RANGE/DEPEG_SHOCK_RANGE/DEPTH_STRESS_KAPPA/PATH_STEPS` -- the M8 stress grid;
  `kappa` (depth-under-stress multiplier) is an assumption, not a measurement.
- **The modeled walk-away shock is -10% for every market with a breakpoint (WBTC, cbBTC, wstETH,
  USR, PST) -- moved here, not into Findings, because it contradicts the observed 97-99%
  profitable liquidation volume on the same three liquid markets.** The model assumes every
  liquidatable position dumps into a single venue's depth simultaneously; real liquidators act
  individually, often same-block, against whatever's actually available (DEX and otherwise). This
  needs aggregator-level depth and a multi-step calibration before the number means what it looks
  like it means. The stranding-count bug that made this figure meaningless in an earlier pass
  (counting healthy, never-at-risk positions as "stranded") is fixed (`tests/test_stress.py`); the
  model's own aggregate-dump assumption is the part still unresolved.
- `PRICE_HISTORY_CHUNKS`, `PRICE_GAP_TOLERANCE_DAYS` -- BTC price-history stitching and
  gap-exclusion tolerance for the EL tail-frequency estimate.
- `LIQ_EDGE_LOOKBACK_DAYS=730`, `LIQ_EDGE_DELAY_SAMPLE_PER_MARKET=30` -- the 24-month window and
  the sampling cap on the (now-retired for 5 markets) delay search; cheap per-event metrics (gas,
  profitability, insolvency) still run over every one of the 886 real events, not a sample.
- `LIQ_EDGE_DUST_THRESHOLD_USD=$1,000` -- below this, gas dominates almost by construction.
- `LIQ_EDGE_HEDGE_COST_BPS=5` -- the BTC/ETH-family exit model assumes a perp hedge at this flat
  cost; no real perp-funding feed is integrated.
- `LIQ_EDGE_REDEMPTION_COOLDOWN_DAYS/HAIRCUT_BPS` -- RLP, USR, wstUSR, apyUSD's issuer-redemption
  cooldown and fee are **not estimable**, confirmed by web research, not computed from a guess:
  RLP/USR (Resolv) gate redemption on a 110% collateralization ratio, not a time window; wstUSR
  was the collateral an attacker used to extract ~$25M from Morpho markets in Resolv's 2026-03-22
  exploit (a compromised signing key, confirmed via Chainalysis, Rekt.news, The Block, and
  Cointelegraph) -- Resolv states the collateral pool remains fully backed, but this project could
  not independently verify ordinary-holder redemption terms for wstUSR in that period; apyUSD has
  a real but unquantified on-chain unlock delay. Only fxSAVE's own primary-source docs support
  near-instant redemption (`cooldown=0`, used with low confidence).
- `LIQ_EDGE_COST_OF_CAPITAL_ANNUAL_FALLBACK=5%` -- used only if the live Aave USDC rate read
  fails; the real run used the live rate (3.88%, `spread.aave_usdc_supply_rate_90d_avg`).
- No smart-contract risk, no USDC depeg (assumed at par throughout), gas receipts and the ETH/USD
  Chainlink feed are trusted as read; a failed read is excluded, never defaulted to zero.

## Next steps

- A calibrated stress engine: real aggregator-level depth per collateral and a multi-step
  liquidation-ordering model, to replace the single-venue aggregate-dump assumption flagged above.
- The funded carry trade (Aave-borrow-against-collateral into a Morpho deposit) vs. a basis trade
  on the same collateral, to see which actually captures more of these markets' residual spread.
- Relative value across markets carrying the same structural risk (same oracle class, same DEX
  depth, same collateral class) instead of one market at a time.
- A delay-reconstruction method that accounts for intermediate position changes (partial
  liquidations, supply/withdraw) instead of assuming a fixed breakeven across a long window --
  needed before AZND/RLP/USR/wstUSR/PT-RLP-9APR2026's delay can be reported at all.

## Reproduce

```
python run_all.py              # M1/M2: universe + positions from chain + API
python build_report.py         # M5-M12: oracle/dex/scenarios/stress/el/spread/optimize/stream_case + figures
python build_cross_section.py  # M10: wider USDC-market cross-section + regression
python build_liq_edge.py       # Task 4: liquidation economics, delay, insolvency, left-on-the-table
pytest                         # all tests pass except test_t6 (confirmed xfail, see below)
```

**T6 fails on purpose.** It fits `dex.py`'s slippage power law on a subset of real quotes and
checks the prediction on a held-out size, within a 15% tolerance; real error is larger because
the pool's effective exponent isn't constant across its own depth. Marked `xfail(strict=True)`,
not deleted or loosened, so this stays a visible, honest result.

Needs `RPC_URL` in `.env` (archive-capable Ethereum mainnet; see `.env.example`).
`config.SNAPSHOT_BLOCK` is pinned; the DEX, DefiLlama, Aave, Chainlink, and Arbitrum reads are
live at whatever block each call is given, not the snapshot alone.

```
defi-liquidation-risk/
  config.py, run_all.py, build_report.py, build_cross_section.py, build_liq_edge.py
  src/kvr/  chain.py api.py universe.py positions.py morpho_math.py irm.py
            oracle.py dex.py scenarios.py stress.py el.py spread.py optimize.py
            stream_case.py liq_edge.py plots.py
  tests/    test_chain.py test_universe.py test_toy.py test_optimize.py test_dex.py
            test_stress.py test_build_report.py test_review_fixes.py test_el.py
            test_spread_cross_section.py
            test_t1_positions.py test_t2_health_factor.py test_t3_irm.py
            test_t4_liquidations.py test_t6_slippage_holdout.py test_t8_t9_liq_edge.py
  data/processed/, reports/figures/
```
