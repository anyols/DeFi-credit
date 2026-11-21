# Keyrock USDC (Morpho): what the yield pays for

Snapshot: block 26,465,719 (Ethereum mainnet, 2026-11-21). Markets: 4. Vault TVL: $53,896
(`0x6C26793c7F1e2785c09b460676e797b716f0Bc8E`, "Keyrock USDC", unlisted).

Note on scope: `morpho_math.py`, `irm.py`, `stress.py`, and this memo's judgment calls were
written by Claude Code at the user's explicit request to reallocate SPEC.md's hand-written-math
ownership split, given time constraints, after the tradeoff (SPEC.md section 8's
whiteboard-derivation framing) was flagged. Every formula is still tested against real on-chain
and API data, not just transcribed from the spec (see `tests/`).

## TL;DR

- The vault's one actually-funded market (USDC/sNUSD, $17.3k) pays **3.14% while Aave's USDC
  rate is 3.90%** -- a **-77bps spread against risk-free**, on a market with an oracle that can't
  see a depeg, zero Uniswap V3 depth, and 100% utilization on every single day of the past 90.
  Its two approved-but-unfunded siblings (USDC/cbBTC, USDC/WBTC, combined ~$600M of unused cap)
  also pay less than risk-free, by -13bps and -12bps.
- **Weakest market: USDC/sNUSD.** `EXCHANGE_RATE` oracle (no price feed at all -- see
  `oracle.classify`), confirmed zero Uniswap V3 depth at any size/fee tier for sNUSD->USDC, and a
  debt-weighted distance-to-liquidation of essentially 0% (fig 1) -- the whole book is already at
  the edge. USDC/WBTC is the only market with enough real Uniswap V3 depth to even compute a
  liquidation breakpoint, and it's a mild **-10% shock**, well before economic losses become
  visible (fig 2) -- see "the gap vs gradual paths overlap" below.
- **Risk-priced allocation vs current:** for a hypothetical $1M budget, the risk-priced optimizer
  puts $800k in USDC/cbBTC and $150k in USDC/WBTC (fig 5) -- markets the vault currently has
  **zero dollars** deployed into despite $500M+/$100M+ of approved, unused cap. The binding
  constraint isn't a risk tradeoff the model found; it's that the vault simply hasn't deployed
  its capital. The $17.3k it does have out is in the single worst-scoring market of the four.

## Findings

**Oracle + liquidity profile, all 3 real markets (figs 1, 6):**
USDC/cbBTC and USDC/WBTC classify `MARKET` (real Chainlink feeds: cbBTC via "BTC / USD";
WBTC via "WBTC / BTC" -> "BTC / USD" -> "USDC / USD"). USDC/sNUSD classifies `EXCHANGE_RATE`
with zero feeds at all -- its price is purely a vault-conversion rate, so a secondary-market
depeg is invisible to it, structurally identical to the oracle risk that let Stream's xUSD bad
debt go unflagged (see Backtest, below). Separately, and more surprisingly: **USDC/cbBTC also
has no usable Uniswap V3 depth** at this block (quoted output barely moves across a 400x size
range, 0.5-200 BTC) -- a real pool-liquidity finding, not a bug (confirmed by direct
`quoteExactInputSingle` calls). Only USDC/WBTC has a fittable slippage curve.

**The liquidation breakpoint (fig 2, hero chart), USDC/WBTC only (no fit exists for the other two):**
At a mild **-10% shock**, just 14 of 637 positions go liquidatable -- but their combined ~40 BTC
of collateral already pushes the aggregate Uniswap V3 slippage to 9.7%, above the LIF breakeven
of 4.2% (`LIF = min(1.15, 1/(0.7+0.3*lltv)) = 1.0438` at this market's 86% LLTV). The whole batch
is stranded. Economic losses stay near zero through -30% and only become material at -45% to
-60% (fig 2) -- **the operational breakdown (liquidators walking away) happens 20-50 points of
shock before a naive loss number would show anything wrong.** The gap (K=1) and gradual (K=6)
paths are visually identical in fig 2: gradual unwinding only helps if liquidators can act
profitably at an early, mild step, and here they can't -- they're already stranded at the first
step that reaches -10%.

**Spread waterfall (fig 3):** all three real markets pay a negative spread over the 90d Aave
USDC rate (3.90%). The EL and liquidity components in the waterfall are small and do not explain
the gap -- the residual carries almost the entire -77/-13/-12bps. Read this as the model
under-measuring risk, not as proof the gap is "pure alpha." Two distinct gaps sit behind that:
(1) USDC/WBTC has an EL estimate, but it computes to ~1.7e-8 bps/yr, functionally zero, because
EL uses a 1-day shock horizon applied uniformly across a grid that goes to -60%, and BTC has
essentially never moved -60% in a single day (0 occurrences in 500 days of history) even though
multi-week drawdowns of that size are common -- the 1-day lens assigns these scenarios near-zero
annual frequency. A correct EL would scale the horizon with the shock size (SPEC.md's own
lambda(x) is horizon-dependent); this version doesn't, and likely understates EL by a wide
margin. (2) USDC/cbBTC has no EL estimate at all, for an unrelated reason: it has no usable
Uniswap V3 depth, so there's no stress sweep for EL to weight in the first place. USDC/sNUSD
also has none, for a third, more fundamental reason: not a thin-data problem but a wrong-data
one -- DefiLlama's price series for it is a smooth accounting NAV, not secondary-market trading
data (see `scenarios.fetch_daily_prices`'s docstring), so using it to estimate depeg frequency
would be circular, not merely noisy.

**Capacity curve and allocation (fig 5):** blended net yield falls from 4.6% at a $1M budget to
2.2% at $50M, as larger deposits dilute utilization in the same few markets. The real finding
isn't the curve's shape, it's the current-vs-optimal bars: the vault's actual deployment (~$17k,
all in the worst-scoring market) looks nothing like what a $1M risk-priced allocation would do
(mostly USDC/cbBTC and USDC/WBTC). This vault is too early-stage for "reallocate for more yield"
to be the right framing -- it simply hasn't deployed the capital it has caps for yet. Cross-
checking the optimizer with SLSQP from several starting points surfaced something more
interesting than agreement: on USDC/sNUSD specifically, SLSQP lands on a $309 allocation
claiming 107,735% implied APY (a "income" of $333k from $309), because that market's rateAtTarget
is pinned at the IRM's own ceiling from 90 days at ~100% utilization, and the optimizer's own
"hold borrow demand fixed" assumption breaks down completely there -- no real borrower keeps
paying as the implied rate explodes. Excluding that one degenerate market, SLSQP and greedy agree
on the objective to within 0.02% (`tests/test_optimize.py`), so the solver itself is sound; the
sNUSD spike is a fact about that specific market, not a bug in either algorithm.

**Cross-section (fig 4):** 66 Ethereum USDC markets with supply > $1M (n=42 after excluding
markets with no usable supply-APY reading or an implausibly pinned one). `spread ~ illiquidity +
oracle_class + collateral_class` gives R^2=0.40 -- illiquidity's coefficient is large and
correctly signed (more time pinned at the utilization cap correlates with more spread), but
oracle_class's coefficient is confounded with collateral_class (most BTC/ETH markets happen to
use real price feeds; most stable/yield-bearing markets don't), so it shouldn't be read as "real
feeds pay more because feeds are good." Restricting to the 12 markets where an EL proxy is even
computable (BTC/ETH-LST collateral; everything else lacks usable tail history) makes the point
visually in fig 4's right panel: EL proxy is 0.0 for every single one of them, because the same
1-day shock-horizon limitation found on the vault's own USDC/WBTC market applies market-wide, not
just to one position. With n=42-66 this is descriptive, not causal, exactly as SPEC.md expects.

**Why Aave->Morpho isn't arbitrage:** naively, USDC/WBTC's 90d supply APY (3.79%) sits above
Aave's current USDC variable borrow rate (3.65%), a 13bps gap that looks like free money. The
funded version of that trade -- a trader who already holds WBTC borrows USDC against it on Aave
at 86% LTV (this market's LLTV) and deposits that USDC into Morpho -- nets
`0.86 * (0.0379 - 0.0365) = 11.4bps` (`spread.aave_morpho_funded_carry`), not 13bps, and that
thin remainder is compensation for Aave's own liquidation risk on the collateral plus Morpho's
credit risk on the supply side -- the same credit risk this whole project exists to price, not
an exploitable gap.

## Backtest: Stream / xUSD (Nov 2025)

Reduced scope (SPEC.md section 7's own cut order, applied given time): oracle class +
concentration only, no full stress run. The market with real size is on **Arbitrum**, not
Ethereum (`0x9e90aec7d768...`, USDC/xUSD, $287.6M borrowed as of this writing) -- found via the
Morpho API, read via Arbitrum's public RPC since the archive RPC connected for the rest of this
project is Ethereum-only. At the pre-announcement block (2025-11-02, found by binary search:
395,834,395), the oracle classifies `UNKNOWN` -- none of MorphoChainlinkOracleV2's standard
fields (feeds or vaults) resolve on this contract at all, the model's explicit "flag for manual
review" case. Concentration (fig 6): 2 borrowers, **HHI ~= 1.0, top-1 share 99.9998%** -- for
practical purposes a single-borrower market. What actually happened, read directly from the
Morpho API rather than assumed: of the market's **$287.6M still borrowed**, Morpho's own
mark-to-market shortfall estimate (`badDebt`) is **$281.9M (~98% of the book)** -- but
`realizedBadDebt` is still $0, and only one `Liquidate` event has ever fired, writing off a
token $187.57. The bulk of the damage is sitting unrealized, not yet crystallized through an
actual liquidation -- a single, massively concentrated position that both can't be liquidated in
the normal sense (no real counterparty, no functioning price discovery) and hasn't been formally
written down. Two cheap, static checks (oracle class + concentration) would have flagged this
market as extreme before a single stress scenario was run. What the model cannot see and doesn't
claim to: the off-chain fund manager's trading losses that caused the depeg in the first place --
it flags the structural vulnerability (opaque oracle, single borrower, de facto unbounded
leverage to one counterparty), not the event.

## What would break this model

- `kappa` (depth-under-stress multiplier) and the slippage power-law fit itself are assumptions,
  not measurements -- and for 2 of the vault's 3 real markets, there was no usable Uniswap V3
  depth to fit at all, so even the "assumption" has no real curve to multiply.
- Liquidations are modeled as full closes; partial liquidations, MEV ordering, and
  liquidator-vs-liquidator competition within a step are not. Gas is a flat assumption
  ($50 in loan-token units), not a live gas-price model.
- No smart-contract risk, no USDC depeg (USDC is assumed at par throughout), and Keyrock itself
  is never modeled as a liquidator (only as a passive depositor).
- EL's 1-day shock horizon (see Findings) likely understates EL for the larger shocks in the grid
  by a material amount; this is the single biggest known gap in the quantitative results.
- The Stream/xUSD concentration numbers are read from the Morpho API's *current* indexed state,
  not a true block-395,834,395 position snapshot (oracle class and realized bad debt are genuine
  point-in-time/historical reads; concentration is a reasonable proxy, not verified pinned).
- The cross-section's EL proxy is aggregate (market-level collateral/debt, no per-position
  distribution, no LIF/slippage) and uses BTC's own exceedance frequency as a stand-in for
  ETH/LST collateral too -- a real approximation, and it inherits the same 1-day-horizon
  understatement as the vault's own EL numbers (visibly: 0.0 for all 12 markets it's computed
  for, fig 4). The regression's oracle_class coefficient is confounded with collateral_class
  (most BTC/ETH markets use real feeds, most stable/yield markets don't) and shouldn't be read
  as a clean causal effect.

## What I'd build next with desk data

- A multi-horizon lambda(x) (1d/7d/30d) so EL stops conflating "never happens in a day" with
  "never happens."
- A second DEX/aggregator quote source (1inch, Curve, CoW) for sNUSD and cbBTC specifically,
  since Uniswap V3 alone shows ~zero depth for both but real liquidations against sNUSD have
  happened (46 of them, replayed exactly against the LIF formula in `tests/test_t4_liquidations.py`)
  -- liquidators are evidently using a venue this model doesn't cover.
- A sequential, MEV-aware liquidation-ordering model instead of one shared per-step slippage
  figure, to see whether the first liquidator in each batch actually clears profitably even when
  the aggregate doesn't.
- A multi-horizon lambda(x) for the cross-section's EL proxy too, and per-collateral-asset price
  history instead of reusing BTC's for the whole ETH/LST bucket.
- A true historical position snapshot for the Stream/xUSD concentration numbers (needs Arbitrum
  archive access at block 395,834,395, not just a public RPC's current-tip reads).

## Reproduce

```
python run_all.py           # M1/M2: pulls the vault's markets and positions from chain + API
python build_report.py      # M5-M12: oracle/dex/scenarios/stress/el/spread/optimize/stream_case + figures
python build_cross_section.py  # M10 cross-section: all Ethereum USDC markets with supply > $1M
pytest                      # T1, T2, T3, T4, T7 pass against live data; T6 is a confirmed xfail (see below)
```

**T6 fails on purpose.** It fits `dex.py`'s slippage power law on a subset of real WBTC/USDC
Uniswap V3 quotes and checks the prediction on a held-out size, within SPEC.md's 15% tolerance.
Real error is 28-41% depending on which size is held out, because the pool's effective exponent
genuinely isn't constant across its own depth (see Findings) -- a real concentrated-liquidity
pool, not a textbook passive AMM. Marked `xfail(strict=True)` rather than deleted or loosened,
so this stays a visible, honest result.

Needs `RPC_URL` in `.env` (archive-capable Ethereum mainnet; see `.env.example`).
`config.SNAPSHOT_BLOCK` is pinned; re-running later re-derives everything else from that same
block, except `build_report.py`'s DEX/DefiLlama/Aave/Arbitrum reads, which are live.

```
keyrock-vault-risk/
  config.py, run_all.py, build_report.py, build_cross_section.py
  src/kvr/  chain.py api.py universe.py positions.py morpho_math.py irm.py
            oracle.py dex.py scenarios.py stress.py el.py spread.py optimize.py
            stream_case.py plots.py
  tests/    test_chain.py test_universe.py test_toy.py test_optimize.py
            test_spread_cross_section.py
            test_t1_positions.py test_t2_health_factor.py test_t3_irm.py
            test_t4_liquidations.py test_t6_slippage_holdout.py
  data/processed/, reports/figures/
```
