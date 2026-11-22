# Where DeFi lending risk actually sits: not where prices move, but where nobody can sell

Snapshot: block 26,465,719 (Ethereum mainnet). Universe: top 15 USDC Morpho Blue markets by
supply (`config.TOP_N_MARKETS`, `config.MIN_SUPPLY_USD=$5M`), of which 5 are zombies -- tracked
in their own section below, not treated as live markets for any yield/risk comparison. The next 5
non-zombie, non-broken-oracle markets by supply (ranks 16-20: PT-USD3-17DEC2026, steakEURCV,
PRIME, weETH, dCOMP) are referenced where relevant; their own 24-month liquidation activity is
negligible (0, 1, 0, 1, 3 events respectively) and changes none of the numbers below. Liquidation
data: last 24 months (`config.LIQ_EDGE_LOOKBACK_DAYS=730`), 886 real `Liquidate` events across
the original 15-market set.

## TL;DR

1. **Liquid collateral (WBTC/cbBTC/wstETH): 609 liquidations over 24 months, 97-99% of volume
   liquidated profitably** (WBTC 99.3%, cbBTC 97.4%, wstETH 99.6% -- `liq_edge.profitability_stats`,
   volume-weighted, dust excluded), **median delay < 1 hour.** Liquidation works here; no edge
   for anyone.
2. **$9.0M of real lender principal is stuck in the 5 zombie markets** (RLP, USR, wstUSR, PT-RLP,
   AZND); reported supply shows **$1.22B** because ~800% APR (`spread.onchain_supply_apr`,
   measured from the on-chain IRM) accrues continuously on debt nobody repays -- **$1.21B of that
   $1.22B (99.3%) is accrued interest, not deposits** (`spread.zombie_principal`: net principal =
   sum(Supply) - sum(Withdraw) from every real transfer event). The delay-to-liquidation figures
   this project first computed for these markets did not survive a direct health-factor check
   (see Findings) and are excluded here. What's verified: the RLP market crystallized **$1.18M of
   bad debt** from a position that was already ~38% underwater before anyone could act, because
   RLP has zero DEX depth at any size or fee tier (`dex.best_route`).
3. **The model flags these structurally in advance**, before any loss prints: oracle class, DEX
   depth, and borrower concentration are all computable from indexed state alone, no stress run
   needed -- the same two checks (oracle class + concentration) flagged Stream/xUSD before its
   Nov 2025 collapse (see Backtest). In markets like RLP/USR the only exit is the issuer, so the
   risk is the issuer, not the collateral price -- and this project could not verify that
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

**Zombie markets, principal vs accrued interest (fig 10):** 5 of 15 markets sit at >=99%
utilization with no Repay event in 30+ days, reporting $1.22B of supply combined (62% of this
universe's $1.97B). Decomposed per market (`spread.zombie_principal`) into net principal
(sum(Supply) - sum(Withdraw), USD, from every real transfer event) and accrued interest (the
remainder):

| Market | Reported supply | Net principal | Accrued interest | Debt when it got stuck | Stuck since |
|---|---|---|---|---|---|
| wstUSR | $770.4M | $5.86M | $764.5M | $6.23M | 2026-03-23 |
| RLP | $255.7M | -$2.96M | $258.7M | $2.64M | 2026-03-23 |
| AZND | $109.9M | $7.35M | $102.5M | $8.23M | 2026-06-23 |
| PT-RLP-9APR2026 | $49.8M | $0.42M | $49.4M | $0.48M | 2026-03-23 |
| USR | $36.6M | -$1.63M | $38.2M | $0.28M | 2026-03-23 |
| **Total** | **$1,222.4M** | **$9.0M** | **$1,213.4M** | | |

RLP and USR show *negative* net principal -- not negative real money, but a limitation of the
cash-flow formula itself: a Withdraw event's `assets` already includes whatever interest accrued
on that lender's shares, while a Supply event is principal-only, so a market with enough
historical turnover (RLP: $17.86B of gross Supply+Withdraw flow against $255.7M of current
supply) can show withdrawals nominally exceeding deposits even though the market plainly still
holds real value. Reported literally, not clamped to zero, because hiding it would be its own
kind of invented number. "Debt when it got stuck" is each market's `borrowAssetsUsd` on the
first day its utilization crossed 99% -- $0.28M to $8.23M across the five, one to two orders of
magnitude below today's reported supply in every case; nearly all of that growth happened after
the market was already stuck, as accrued interest with no real economic activity behind it.

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
- `ZOMBIE_PRINCIPAL_LOOKBACK_DAYS=3yr` -- how far back the principal decomposition pulls
  Supply/Withdraw history; comfortably covers Morpho Blue's full history for every market here.
  The net-principal formula itself (sum(Supply)-sum(Withdraw)) goes negative for RLP and USR --
  a real limitation of subtracting principal-only inflows from interest-inclusive outflows on a
  market with heavy historical turnover, not a sign of negative real money (see Findings).
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
  exploit (a compromised signing key, not a contract flaw), confirmed independently via:
  [Chainalysis](https://www.chainalysis.com/blog/lessons-from-the-resolv-hack/),
  [Rekt.news](https://rekt.news/resolv-labs-rekt),
  [The Block](https://www.theblock.co/post/394582/resolvs-usr-stablecoin-depegs-after-attacker-mints-80-million-unbacked-tokens-extracts-roughly-25-million),
  [Cointelegraph](https://cointelegraph.com/news/resolv-protocol-temporarily-halted-80m-exploit) --
  Resolv states the collateral pool remains fully backed, but this project could not independently
  verify ordinary-holder redemption terms for wstUSR in that period; apyUSD has a real but
  unquantified on-chain unlock delay. Only fxSAVE's own primary-source docs support near-instant
  redemption (`cooldown=0`, used with low confidence).
- `LIQ_EDGE_COST_OF_CAPITAL_ANNUAL_FALLBACK=5%` -- used only if the live Aave USDC rate read
  fails; the real run used the live rate (3.88%, `spread.aave_usdc_supply_rate_90d_avg`).
- No smart-contract risk, no USDC depeg (assumed at par throughout), gas receipts and the ETH/USD
  Chainlink feed are trusted as read; a failed read is excluded, never defaulted to zero.

## Lending vs. the ETH basis trade (fig 12)

A comparison, not a strategy claim: over the same 18 months, 1x basis (long wstETH + short ETH
perp: staking yield + funding - fees) averaged 5.89%/yr vs. Aave's 3.88%/yr, but with real tail
risk (11% of days negative, worst 30-day stretch -1.50%/yr annualized). Levering it 3.33x via a
Morpho USDC borrow against wstETH (70% target LTV, 86% LLTV) raises the mean to 9.45%/yr but
*lowers* the Sharpe ratio (0.36 vs. 0.42 unlevered) -- the borrow-cost drag amplifies volatility
faster than return. A +10% ETH move alone would require posting 33% of the trade's equity as
additional perp margin, separate from the Morpho position entirely.

## Next steps

- A calibrated stress engine: real aggregator-level depth per collateral and a multi-step
  liquidation-ordering model, to replace the single-venue aggregate-dump assumption flagged above.
- A delay-reconstruction method that accounts for intermediate position changes (partial
  liquidations, supply/withdraw) instead of assuming a fixed breakeven across a long window --
  needed before AZND/RLP/USR/wstUSR/PT-RLP-9APR2026's delay can be reported at all.

## Reproduce

### Interactive Streamlit demo

```sh
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

The app opens on the four research tracks. Its other tabs let you rerun the lender
backtest, vary recovery assumptions, inspect the two published trades, stress five
collateral markets, and calculate ETH basis carry and short-leg margin exposure.
For an interview, try replaying with $1M of starting capital and $200k of minimum
entry liquidity, then compare 0% and 50% recovery.

The app uses the bundled compressed snapshots in `data/processed` and runs without
RPC credentials or live data queries. The historical replay inputs and stress position
snapshots can be repackaged with `python build_demo_data.py` after refreshing the
historical replay cache. Repackaging uses an archive RPC for block-pinned market totals
and oracle prices. For Streamlit Community Cloud, use `streamlit_app.py` as the entry
point and include the processed snapshots and research figures with the source.

### Point-in-time lender replay

**Aave finished ahead of all three Morpho allocation rules across all four recovery scenarios.**
The weekly replay starts with $10M on 2025-05-22 and attempts to exit on 2026-11-21.
The Morpho API returned 760 Ethereum USDC markets created by the cutoff. Of these, 236 had
at least one historical observation with $500k of supply before the exit date, and 225
qualified for ranking at least once. Eligibility uses lagged supply at each rebalance and
requires seven complete daily rate observations. No end-of-window supply or DEX screen
enters the ranking.

| Strategy | Modeled cash after exit | Residual face claim | Equity at 25% recovery | Equity at 50% recovery | Equity at 100% recovery |
|---|---:|---:|---:|---:|---:|
| Greedy rate ranking | $0 | $700.83M | $0.97M | $1.94M | $3.88M |
| Add utilization and repayment screen | $0 | $373.15M | $0.57M | $1.15M | $2.30M |
| Add free-liquidity screen | $2.63M | $19.60M | $3.26M | $3.90M | $5.18M |
| Aave v3 USDC | $10.57M | $0 | $10.57M | $10.57M | $10.57M |

Modeled cash after exit is the 0% recovery case. The higher scenarios apply their
recovery rates to the portion of each residual claim covered by the market's indexed
collateral-to-debt ratio, capped at par. Missing collateral marks receive zero recovery.
This uses aggregate collateral marks. It assumes a pro-rata allocation and leaves
borrower-level recoveries, exit prices, execution costs, and recovery timing unmodeled.
Face claims include accrued interest on positions the replay could not withdraw.

**What each rule added.** Greedy targets up to five equal sleeves ranked by seven-day
trailing supply APR. The repayment screen requires 30 daily observations and excludes a
market when utilization was at least 99% on 14 or more of those days and there was no
positive Repay event. It reduced residual face claims but produced lower collateral-marked
recovery than greedy in this window. The liquidity screen also requires prior-day free
USDC of at least $2M, the initial sleeve size. This rule improved exit proceeds and still
left most reported exposure trapped. These are in-sample comparisons. The original
snapshot oracle and DEX screen remains in the separate exploratory backtest
([legacy results](data/processed/backtest.json)).

**Worked trade before deterioration.** On 2025-06-19, PT-syrupUSDC-28AUG2025 with market ID
`0x3bdb5805…` ranked at 11.98% trailing APR. The 2025-06-18 input showed 88.91% utilization,
a recent repayment, and $870k of free USDC. Greedy added $1.49M and the repayment-screen
strategy added $2.01M. The liquidity-screen strategy did not select this market. It did
select another Morpho market with the same collateral symbol. The first market reached
99% utilization on 2025-08-26 and was at or above that level on 21 of the following 30
observations including the crossing. Greedy and the repayment-screen strategy had no
claim left in this market at the end. They withdrew $2.05M and $2.01M respectively in
later rebalances, including balances already held on the decision date. Terminal free
liquidity was $0.83. This example shows what the liquidity screen saw early and why one
blocked allocation cannot establish an avoided loss.

**Worked trade in an already distressed market.** On 2025-05-22, greedy put $2M into
PAXG market `0x8eaf7b29…` at about 800% trailing APR. The previous day's data showed
only $19.69 of free USDC, 30 days near full utilization, and no positive Repay event.
Both screens rejected it. The greedy replay made no withdrawals from that market.
Archive RPC checks matched indexed free USDC on the input and terminal dates for both
worked markets to within one USDC base unit.

The replay ranks on prior-day and earlier inputs, uses the execution day's indexed
opening liquidity for fills, and books that day's interest and bad debt after trading.
It approximates execution from daily snapshots, charges a flat $50 per Morpho action,
and models own-deposit rate dilution against historical borrow demand. Withdrawals are
capped by observed external free USDC. The simulator does not credit unobserved
liquidity from its own deposits or model borrowers' response to those deposits. This is
a conservative exit convention. Idle cash earns zero, the replay assumes zero Morpho
market fees, and the Aave benchmark assumes full exit at par with no trading friction.

```
python build_historical_replay.py --refresh --spot-check
python build_historical_replay.py  # rerun offline with the cached inputs
```

The refresh requires Morpho, DefiLlama, and the configured Ethereum archive RPC.
Inputs are cached in `data/raw/historical_replay.json`. The
[replay results](data/processed/historical_replay.json) include recovery sensitivity,
worked trades, and the RPC checks when run with `--spot-check`.

### Original analysis

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
