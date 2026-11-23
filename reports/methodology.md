# Research methodology

## What the results measure

- **Historical replay** covers 2025-05-22 to 2026-11-21, using 225 markets that ranked at least once. At each weekly rebalance the strategies rank by seven-day trailing supply APR using prior-day and earlier inputs. The first screen excludes persistent near-full utilization without a recent positive repayment, and the second requires at least $2M in prior-day free USDC. Fills use the execution day's opening liquidity. Own-deposit dilution holds historical borrow demand fixed. Withdrawals are bounded by observed external free USDC, with no credit for hypothetical unspent deposits. Positions pay a flat $50 per action, idle cash earns zero, and the replay assumes zero Morpho market fees.
- **Terminal values** distinguish nominal accrued claims, cash obtainable under the modeled exit convention, and recovery scenarios capped by indexed collateral coverage. The 0%, 25%, 50% and 100% recoveries are scenarios. At 50% recovery, Aave would have to realize only **37.0%** of its modeled terminal value to tie the best Morpho rule. This is a break-even calculation. The benchmark uses daily compounding from DefiLlama's Aave base APY, $50 each for entry and exit, and assumes withdrawal at par with no modeled capacity limit.
- **Liquidation economics** cover 886 events in the 15-market Ethereum sample, 2024-11-21 to 2026-11-21 UTC. Modeled P&L uses the execution-block oracle mark, the transaction receipt's whole gas cost converted using ETH/USD, and a collateral-specific exit assumption. BTC and ETH use a flat 5 bp hedge cost and zero oracle-to-exit price difference in the model. Actual swaps, funding, inventory unwind and private payments are unavailable. A missing gas or exit assumption leaves modeled P&L unpriced. Current depth does not enter historical profitability estimates. The original fixed-breakeven timing estimate failed when positions changed, including after a partial liquidation. The RLP timeline uses the recorded event dates and amounts.
- **Zombie-market balances** use accrued protocol state at Ethereum block 26,465,719. The classifier requires at least 30 consecutive near-full-utilization days and no repayment in 30 days. Reported supply includes accrued claims. Historical net cash flows cannot identify remaining deposited principal because withdrawals can include interest. The report therefore uses historical debt at the start of each observed streak and snapshot withdrawal liquidity, and does not estimate outstanding principal or split supply into principal and interest.
- **Stream/xUSD** is retrospective in the published snapshot. The historical path discovers pre-cutoff borrowers and requires their on-chain borrow shares to reconcile to the historical market total. Available Arbitrum RPCs could not serve that state at block 395,834,395, so later borrower concentration is kept explicitly retrospective.
- **Stress, spreads and allocation** are conditional models. The held-out slippage fit still misses its 15% tolerance. Expected loss is sensitive to the tail proxy and the liquidity premium is an assumed charge. The legacy relative-value run uses current market selection and snapshot exit filters, so it is exploratory. The separate ETH basis calculation models funding, carry and margin exposure.

## Liquidity-adjusted spread experiment

The final 183 days form a separate retrospective evaluation, from 2026-05-22
through exit on 2026-11-21. All five strategies start with $10M. No parameters
are fitted. Earlier snapshots warm up the signals. The broader dataset was
already inspected, so this is not a blind out-of-sample test.

All five strategies start in Aave and share the same cash ledger, transfer
cost and execution assumptions. The three Morpho comparators preserve their
ranking and screening rules. They rank eligible markets with enough lagged
free USDC to exceed gas. The original full-window replay remains a separate
experiment without the shared Aave sweep or fixed-size execution overlay.

The spread rule compares one week's carry on each fixed $2M sleeve against
Aave carry using the seven daily observations strictly before the decision.
Morpho interest includes own-deposit dilution. Entry requires passing the
repayment screen, full sleeve size in lagged free USDC, collateral coverage
at or above par, and positive excess carry net of four $50 gas actions.
Eligible incumbents stay while gross excess carry is positive and take
precedence over new entries. A failed gate or nonpositive carry triggers an
exit request. Historical executable collateral prices and issuer redemption
capacity are unavailable.

Entries across all Morpho arms are capped by opening free USDC as a
conservative capacity proxy. Opening free USDC also caps withdrawals. For
the spread rule, an actual partial entry must clear the gas hurdle at fill
size. Unallocated cash is swept into Aave. The base case assumes unlimited
Aave capacity and a stress case caps each Aave withdrawal at $1M.
Unwithdrawn Aave claims are marked at par. The recovery scenario applies
only to collateral-covered Morpho claims.

Gross turnover sums transfers across both venues and excludes the terminal
exit. Drawdown and annualized return use the 50% recovery mark. Allocation
shares use nominal held balances and can be dominated by accrued distressed
claims. A claim awaiting exit is the amount remaining above its latest
target after applying the withdrawal cap. Distressed exposure means a
positive position in a market at 99% utilization or above. The decision
examples are the first executed entry, first unchanged hold, and first
blocked exit in simulation order. Later withdrawals can include subsequently
acquired exposure.

Regenerate offline with `python build_historical_replay.py --spread`.
The [saved result](../data/processed/spread_replay.json) includes the reference
run, capacity stress, and sensitivity to a fortnightly cadence and a $4M
liquidity floor. These diagnostics do not change the reference parameters.

## RLP event provenance

Both rows were checked by decoding the Morpho `Liquidate` log in the transaction
receipt and using the enclosing block's UTC timestamp. The event field order
comes from the [Morpho event definition](https://github.com/morpho-org/morpho-blue/blob/main/src/libraries/EventsLib.sol).

| Block | UTC date | USDC repaid | USDC bad debt |
|---|---|---:|---:|
| [25,190,059](https://etherscan.io/tx/0xcce35b622e7ae2bebba258860c9021b38c41343d207c36ed76c0fb3c75bcf40c) | 2026-05-27 | 776,865.782034 | 0 |
| [25,259,134](https://etherscan.io/tx/0x267a017b5f011558223af8aee98cdaf4b6e2418ab204b0a12b8fc892eeec150f) | 2026-06-06 | 0.000002 | 1,181,253.926430 |

## Refresh the underlying research from the repository root

Copy `.env.example` to `.env` and configure an Ethereum archive RPC for network refreshes. Morpho GraphQL, DefiLlama and other public data services must also be available. A process environment variable `ARBITRUM_RPC_URL` can supply a historical Arbitrum endpoint for the Stream case.

| Entry point | Output |
|---|---|
| `python run_all.py` | Market and borrower snapshots |
| `python build_report.py` | Oracle, concentration, stress, spreads, allocation and case-study results |
| `python build_liq_edge.py` | Liquidation events and modeled costs |
| `python build_historical_replay.py --refresh --spot-check` | Pinned inputs, replay and RPC checks |
| `python build_demo_data.py` | Bundled replay and stress data for the app and notebook |
| `python build_backtest.py` | Exploratory relative-value comparison and legacy figure |
| `python build_basis.py` | ETH basis study |

The saved liquidation estimate retains the prior oracle-mark profitability calculation. Event counts, RLP transaction details and recorded bad debt were refreshed independently. Refreshing all historical oracle and gas reads requires sufficient archive RPC capacity.
