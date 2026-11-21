# SPEC — Keyrock USDC Vault: What Does the Yield Pay For?

> Drop this file in the repo root. Tell Claude Code: "Read SPEC.md. Follow the ground rules. We build in the order of §7. Do not start a module before the previous one's validation test passes."

---

## 0. Goal (one paragraph)

Build a reproducible risk engine for the Morpho markets that Keyrock's USDC vault lends into. For each market it answers three questions: (1) under which price shocks does the market take bad debt, given liquidator economics and oracle design, (2) how much of the market's supply APY is compensation for expected loss vs. liquidity vs. unexplained premium, and (3) what is the risk-priced optimal allocation of $B across these markets, including the rate impact of our own deposit. The project also backtests the engine on the Stream Finance / xUSD collapse (Nov 2025).

**One-line headline the memo must produce:**
"Market M's spread over risk-free = EL x bps + liquidity y bps + residual z bps. Under scenario S the vault loses N bps. The risk-priced allocation differs from the current one in market K because of constraint C."

---

## 1. Ground rules for Claude Code

- **Lean.** Plain Python ≥3.11 with `web3`/`eth-abi`, `pandas`, `numpy`, `scipy`, `matplotlib`, `requests`, `pyarrow`. No frameworks, no class hierarchies, no CLI libraries, no Docker. Functions under ~40 lines. No emojis anywhere.
- **Reproducible.** Every on-chain read pinned to `SNAPSHOT_BLOCK` in `config.py`. All raw pulls cached to `data/raw/*.parquet`. One command reproduces everything: `python run_all.py`.
- **Integer-exact where the protocol is.** Replicate Morpho's WAD/1e36 math in integers (Python `int`) for health checks and share conversions. Convert to float only for analytics.
- **Never trust a formula without a test.** Each module has a validation test in `tests/` that compares against on-chain or API truth (§5).
- **Verify, don't assume.** Anywhere this spec says *(verify)*, confirm from the contract ABI, docs, or a live call before coding against it. ABIs come from the morpho-org GitHub repos or Etherscan, not from memory.
- **Arnau writes the core math by hand** (see §1.1). Claude Code reviews and tests those files. It does not rewrite them.

### 1.1 Ownership split

| Arnau writes first (by hand) | Claude Code builds |
|---|---|
| `morpho_math.py`: share math, health check, LIF, bad-debt rule | `chain.py`: RPC, multicall, chunked `getLogs`, caching |
| `irm.py`: AdaptiveCurveIRM curve + drift | `api.py`: Morpho API client (REST + GraphQL) |
| `stress.py`: the liquidation loop | `dex.py`: quote collection + slippage fit |
| `tests/test_toy.py`: hand-computed toy cases | plotting, `run_all.py`, optimizer scaffolding |

Commit in small, honest steps across the weekend. The git log is part of the evidence.

---

## 2. Repo layout

```
keyrock-vault-risk/
  README.md            # = the memo (§6)
  SPEC.md
  config.py            # RPC_URL, SNAPSHOT_BLOCK, VAULT, chain ids, scenario grid
  run_all.py
  src/kvr/
    chain.py           # RPC, multicall, getLogs chunking, parquet cache
    api.py             # Morpho API
    universe.py        # vault -> markets -> MarketParams
    positions.py       # borrower discovery + position state
    morpho_math.py     # (Arnau)
    irm.py             # (Arnau)
    oracle.py          # oracle read + classification
    dex.py             # slippage curves
    scenarios.py       # shock grid + historical frequencies
    stress.py          # (Arnau)
    el.py              # expected loss
    spread.py          # spread decomposition + cross-section
    optimize.py        # risk-priced allocation + capacity curve
    stream_case.py     # xUSD backtest
    plots.py
  tests/
  data/raw/  data/processed/   (gitignored raw)
  reports/figures/
```

---

## 3. Data sources

| What | Source | Notes |
|---|---|---|
| On-chain state + logs | Ethereum RPC with archive access (Alchemy/Infura free tier) | Chunk `eth_getLogs` to provider block-range limits; back off on 429 |
| Morpho Blue singleton | `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb` *(verify)* | `market(id)`, `position(id,user)`, `idToMarketParams(id)` |
| Vault | Keyrock USDC V2 `0x04422053aDDbc9bB2759b248B574e3FCA76Bc145` (Ethereum) | Also list all Keyrock-curated vaults via API (e.g. "Keyrock Prime USDC", which is larger). Pick the primary vault after checking TVL and allocations, and record the choice in config. |
| Vault allocations, market metadata, historical APY/utilization | Morpho API: GraphQL `https://api.morpho.org/graphql` and REST `https://api.morpho.org/v0/...` | Introspect the schema before writing queries. The REST spec has no IRM endpoint, so read `rateAtTarget` on-chain. |
| IRM | Address from each market's `MarketParams.irm` | `borrowRateView(marketParams, market)`, `rateAtTarget(id)` *(verify name)* |
| Oracles | Address from `MarketParams.oracle` | `price()`; for MorphoChainlinkOracleV2 also `BASE_FEED_1/2`, `QUOTE_FEED_1/2`, `BASE_VAULT`, `QUOTE_VAULT`, `SCALE_FACTOR` *(verify)* |
| DEX depth | Uniswap V3 `QuoterV2` via `eth_call` + one aggregator quote API as cross-check | Quote at sizes $50k → $50M |
| Price history (tails) | DefiLlama coins API (`coins.llama.fi`) and/or Binance klines | Daily + hourly for collateral assets; LST/LRT vs ETH ratio history for depeg tails |
| Off-Morpho rates (spread section) | Aave v3 Pool `getReserveData(USDC)` on-chain + DefiLlama yields history | Aave USDC supply rate = on-chain "risk-free" proxy; T-bill rate as a second reference |

---

## 4. Modules and math

### M1 `universe.py`
- Pull the vault's current allocations → list of `(marketId, supplyAssets, cap)`.
- For each id: `idToMarketParams(id)` → `(loanToken, collateralToken, oracle, irm, lltv)`. Check `id == keccak256(abi.encode(MarketParams))`.
- Output: `data/processed/markets.parquet`. Include the vault's share of each market's supply, `vault_supply / totalSupplyAssets`.

### M2 `positions.py`
- **Borrower discovery:** `getLogs` on Morpho Blue for `Borrow` and `SupplyCollateral` with `topic1 = marketId`, from the market's `CreateMarket` block to `SNAPSHOT_BLOCK`. Collect unique `onBehalf`.
- **State:** multicall `position(id, user)` → `(supplyShares, borrowShares, collateral)` at `SNAPSHOT_BLOCK`. Multicall `market(id)` → `(totalSupplyAssets, totalSupplyShares, totalBorrowAssets, totalBorrowShares, lastUpdate, fee)`.
- **Accrue interest** from `lastUpdate` to the snapshot block timestamp, using `borrowRateView` and Morpho's 3-term Taylor compounding *(verify against MorphoBalancesLib)*. Without this, totals drift from the truth.
- Also store concentration metrics per market: top-1 and top-5 borrower share of debt, and HHI.

### M3 `morpho_math.py` (Arnau)
```
VIRTUAL_SHARES = 1e6, VIRTUAL_ASSETS = 1
toAssetsUp(s, A, S)   = ceil(s * (A + 1) / (S + 1e6))
borrowed              = toAssetsUp(borrowShares, totalBorrowAssets, totalBorrowShares)
maxBorrow             = collateral * price / 1e36 * lltv / 1e18      (round down)
healthy               <=> borrowed <= maxBorrow
HF                    = maxBorrow / borrowed
LIF                   = min(1.15, 1 / (0.3 * LLTV + 0.7))          # = 1/(1 - 0.3(1-LLTV))
seized(repaid)        = repaid * LIF * 1e36 / price                 (oracle price)
bad debt              : realized when collateral hits 0 with debt left -> socialized pro rata to
                        that market's suppliers (totalSupplyAssets reduced by badDebtAssets)
```
Also note how a loss reaches the vault's share price, which depends on vault version and adapter (V1.0 realizes it, V1.1 does not, V2 depends on the adapter) *(verify in Morpho bad-debt docs)*. State which applies to the chosen vault.

### M4 `irm.py` (Arnau): AdaptiveCurveIRM
```
u_t = 0.9, c = 4, speed = 50/yr  (verify)
err(u) = (u - u_t)/(1 - u_t)  if u > u_t  else  (u - u_t)/u_t
curve  = (1 - 1/c)*err + 1    if err < 0  else  (c - 1)*err + 1
r_borrow(u) = rateAtTarget * curve            # per second, WAD
r_supply    = r_borrow * u * (1 - fee)
APY         = exp(r * 31_536_000) - 1
drift: rateAtTarget(t) = rateAtTarget(0) * exp(speed * err * t)   # long-run adaptation
```
Sanity checks: u=0 → r_T/4, u=0.9 → r_T, u=1 → 4·r_T. Holding u=1 for 5 days roughly doubles r_T.
**Own-deposit impact:** depositing x moves u from B/S to B/(S+x). Report both the instant impact (curve) and the 30-day impact (drift, assuming borrow demand is fixed).

### M5 `oracle.py`
- Read `price()` at the snapshot. Check that it matches the 1e36 scaling plus the decimal adjustment `(1e36 * 10^(loanDec - collDec))` *(verify)*.
- **Classify each oracle.** This is the key feature:
  - `MARKET`: at least one feed tracks a traded market price (e.g. ETH/USD).
  - `EXCHANGE_RATE`: price comes only from a vault conversion or protocol exchange rate (ERC4626 `convertToAssets`, wstETH/stETH rate). A secondary-market depeg is **invisible** to the oracle.
  - `FIXED`: no feeds and no vault, so the price is constant. Every market drop is invisible.
  - `UNKNOWN`: non-standard contract → flag for manual review.
- Use `feed.description()` to label feeds. Cross-check with the API's oracle data and warnings.

### M6 `dex.py`
- For each collateral asset: quote collateral→loan at sizes `q ∈ {50k, 250k, 1M, 5M, 20M, 50M}` USD. Fit `slip(q) = a * q^b` in log-log (expect b ≈ 0.5).
- Under stress, depth shrinks: `slip_s(q) = kappa_s * slip(q)`, with `kappa ∈ {1, 2, 4}`. **This is an assumption. Label it as one.**
- Aggregate seized collateral **per collateral asset across all markets** in a scenario, because the liquidations sell into the same pools.

### M7 `scenarios.py`
Scenario = (market shock `m`, oracle shock `m_o`, depth stress `kappa`, path `K` steps).
- `m_o = m` for MARKET oracles. `m_o = 1` for FIXED. For EXCHANGE_RATE: `m_o = 1` on depeg-only shocks, `m_o = m_underlying` on underlying shocks.
- Grid: underlying shocks −10% … −60% in 5% steps; depeg shocks for LST/LRT/yield-stable collateral −2% … −80%; `kappa ∈ {1,2,4}`; `K ∈ {1 (gap), 6 (gradual)}`.
- Named scenarios: worst historical 1h / 24h / 7d moves per asset, Oct-10-2025 replay, and "oracle gap" (market −X%, oracle flat).
- **Frequencies:** from price history, estimate annual exceedance frequency `lambda(x)` = expected number of declustered ≤ −x moves per year at horizon h. Use these as scenario weights in M9. For collateral with no usable tail history, mark EL as **not estimable (judgment only)**. Do not invent a number.

### M8 `stress.py` (Arnau): the liquidation loop
For each scenario, for each path step k = 1..K:
```
P_o  <- oracle price after step k;  P_m <- market price after step k
for each position with borrowed > collateral * P_o * LLTV:       # liquidatable
    seize   = min(collateral, borrowed * LIF / P_o)
    proceeds= seize * P_m * (1 - slip_s(total seized notional for this asset at step k))
    profit  = proceeds - repaid - gas
    if profit >= 0:  execute; bad_debt += max(0, borrowed - collateral*P_o/LIF) when collateral exhausted
    else:            no liquidation (liquidators walk away)
end-of-path:
    realized_loss  = sum of bad debt from executed liquidations
    economic_loss  = sum over remaining positions of max(0, borrowed - collateral * P_m * (1 - slip))
market_loss_frac = (realized + economic) / totalSupplyAssets
vault_loss       = sum over markets of vault_supply_i * market_loss_frac_i
```
Outputs per (market, scenario): realized loss, economic loss, number of positions liquidated or stranded, and the **breakpoint shock** where `slip > LIF - 1` makes liquidation unprofitable. That breakpoint is the core insight of the project. Compare gap (K=1) with gradual (K=6).

Also run a **liquidity stress**: withdrawable = `totalSupply - totalBorrow`. Report what fraction of the vault's position could exit if utilization goes to 100%, as happened in the Stream contagion.

### M9 `el.py`
`EL_i (bps/yr) = sum over shock buckets of [lambda(bucket) * loss_frac_i(bucket)]`, using the base `kappa` plus a stressed-depth variant. Report a range, not a point.

### M10 `spread.py`
- **Per Keyrock market:** `spread_i = supplyAPY_i(90d avg) - r_f(90d avg)`. Waterfall: `spread = EL_i + liquidity_i + residual_i`.
- **Liquidity proxy:** % of time with utilization ≥ 99% over 90d (from API history), priced via the cross-section below.
- **Cross-section:** all Ethereum Morpho USDC markets with supply > $1M. Regress `spread_i ~ EL_i + illiq_i + oracle_class + collateral_class`. The residual is "unexplained premium". With n ≈ 30–80 this is descriptive, so say so.
- **"Why Aave→Morpho isn't arbitrage" box:** show the historical Morpho-supply minus Aave-borrow gap, then show the funded trade: `carry = collateral_yield + LTV * (r_morpho_supply - r_aave_borrow)`, subject to liquidation risk on the collateral and credit risk on the Morpho side. Show that the gap tracks EL + illiquidity.

### M11 `optimize.py`
```
income_i(x) = x/(S_i + x) * Bor_i * r_borrow(Bor_i/(S_i + x)) * (1 - fee_i)
maximize    sum_i [ income_i(x_i) - EL_i * x_i ]
subject to  sum x_i <= B,  0 <= x_i <= cap_i (vault caps),  x_i <= alpha * (S_i + x_i),
            sum_i x_i * worst_loss_frac_i <= L_max
```
- **Primary solver:** greedy water-filling in $50k increments by marginal risk-adjusted yield. It's easy to explain in an interview. **Check** with `scipy.optimize.minimize(SLSQP)` from several starting points.
- **Capacity curve:** solve for `B ∈ {1, 5, 10, 25, 50}M` and plot blended net yield vs B.
- Compare with the current Keyrock allocation. Frame it as "the binding constraint is X", **never** as "Keyrock is leaving yield behind".

### M12 `stream_case.py`
- Find xUSD-collateral Morpho markets via the API on every chain where they exist. Set the snapshot to a block on 2025-11-02, before the Nov 4 announcement.
- Run M5 (oracle class), the M2 concentration metrics, and the M8 "oracle gap" scenario.
- **Success criterion:** these markets rank in the top risk decile of the comparable universe at that block. Compare with realized `badDebtAssets` from later `Liquidate` events.
- **State honestly what the model cannot see:** the off-chain fund-manager loss. The model flags the structural vulnerability (fixed price, single borrower, leverage), not the event.

---

## 5. Validation tests (must pass before moving on)

| Test | Check | Tolerance |
|---|---|---|
| T1 positions | Sum of reconstructed borrow assets per market vs accrued `totalBorrowAssets` | rel err < 1e-6 |
| T2 health | HF for 20 random positions vs Morpho API | rel err < 1e-4 |
| T3 IRM | `irm.py` borrow rate vs on-chain `borrowRateView` at snapshot | rel err < 1e-9 |
| T4 LIF | Replay last ~50 `Liquidate` events: `seized ≈ repaid * LIF * 1e36 / price(block-1)` | rounding only |
| T5 vault APY | 30d realized APY from vault share price vs allocation-weighted market supply APY net of fees | explain any gap > 20 bps |
| T6 slippage | Fit predicts a held-out quote size | within 15% |
| T7 toy | Hand-computed single-position markets: healthy, liquidatable-profitable, liquidatable-unprofitable, fixed-oracle | exact |

---

## 6. Outputs

**Figures** (`reports/figures/`):
1. Debt-weighted CDF of "price drop to liquidation" per market.
2. Loss vs shock per market, gap vs gradual, with the slippage > LIF−1 breakpoint marked. **(hero chart)**
3. Spread waterfall per Keyrock market: r_f, EL, liquidity, residual.
4. Cross-section scatter: spread vs EL, colored by oracle class.
5. Capacity curve, plus optimal vs current allocation bars.
6. Stream case: risk-score rank of xUSD markets before the event.

**README.md = the memo (1 page + appendix):**
```
# Keyrock USDC (Morpho): what the yield pays for
Snapshot: block N (date). Markets: k. Vault TVL: $X.

## TL;DR
- <headline sentence with numbers>
- <weakest market + why: oracle / depth / concentration>
- <risk-priced allocation vs current + binding constraint>

## Findings     (figures 2, 3, 5 with 2 lines each)
## Backtest: Stream / xUSD (Nov 2025)   (figure 6, what it caught, what it couldn't)
## What would break this model           (assumptions: kappa, full liquidation, gas, no smart-contract risk,
                                          USDC itself assumed at par, Keyrock as liquidator not modeled)
## What I'd build next with desk data
## Reproduce: python run_all.py           (+ repo map)
```
No adjectives. Every number traceable to a function.

---

## 7. Build order and timeline

| When | Build | Gate |
|---|---|---|
| Sat evening | config, chain, api, M1, M2 | T1 |
| Sun 09–13 | M3, M4, M5, M6, M7, M8 + toy tests | T2, T3, T4, T6, T7 |
| Sun 14–18 | M9, M10, M11 | T5 |
| Sun 18–22 | M12, figures, memo | memo reads in 3 min |
| Mon | polish, re-run from clean clone, send | `run_all.py` passes on a fresh checkout |

**If you run short on time, cut in this order:** (1) gradual path K=6, (2) the cross-section regression (keep only the per-market waterfall), (3) the SLSQP check, (4) Stream case reduced to oracle class + concentration only.
**Never cut:** the validation tests, the LIF-vs-slippage breakpoint, the memo.

---

## 8. Be able to do these at a whiteboard (no notes)

- Derive LIF, and explain why a lower LLTV gives a bigger bonus.
- HF from raw position numbers, including the virtual-shares rounding.
- The IRM curve at u = 0, 0.9, 1, and why supply rate = borrow rate × u × (1 − fee).
- Why a FIXED or EXCHANGE_RATE oracle hides losses, and how that caused Stream's bad debt.
- When liquidators walk away (slippage > LIF − 1) and why aggregation across markets matters.
- How bad debt reaches the vault share price for this vault's version.
- Why the Aave→Morpho gap is mostly credit premium, not arbitrage.
- What the model ignores: off-chain counterparties, smart-contract risk, USDC depeg, liquidator inventory and capital limits.
