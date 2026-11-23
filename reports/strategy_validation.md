# Does the carry rule improve on the liquidity screen?

At the $2M liquidity floor, the spread rule finished **$671k ahead of the liquidity screen** at 50% Morpho recovery and **$946k behind Aave**. Two markets explain nearly all of the gain over the liquidity screen. Both rules targeted those markets on the same dates. The spread rule ended up funding smaller positions at key re-entries. Raising the floor to $4M removed the advantage.

## Reference replay

I reran the bundled daily snapshots and event data from 22 May to 21 November 2026 with $10M, weekly rebalances, five $2M sleeves, and $50 per transfer. All arms start in Aave, and unused cash returns there. The result matched the published reference exactly. At 0% Morpho recovery, modeled equity equals exit cash in this base case.

| Strategy | Exit cash / 0% ($M) | 25% ($M) | 50% ($M) | 100% ($M) |
|---|---:|---:|---:|---:|
| Aave | 10.178 | 10.178 | 10.178 | 10.178 |
| Adjusted spread | 9.018 | 9.125 | 9.232 | 9.445 |
| Liquidity screen | 8.033 | 8.297 | 8.561 | 9.088 |
| Greedy | 2.143 | 3.693 | 5.243 | 8.344 |
| Repayment screen | 0.837 | 2.376 | 3.915 | 6.992 |

The ranking is unchanged across these four base recovery assumptions. Recovery applies to the indexed collateral-covered portion of unwithdrawn Morpho claims. The period is a retrospective evaluation of fixed rules. The broader history had already been seen.

## Where the $671k came from

I reconciled the executed transfers, daily accruals, bad debt, and hypothetical exit settlements. The nominal ledger aggregates once per day. The terminal bridge removes every unwithdrawn claim from cash, then adds its recovery mark once.

| Spread minus liquidity screen | Change in value ($) |
|---|---:|
| Aave interest | +9,347 |
| Nominal Morpho interest | −998,725 |
| Allocated recorded bad debt | 0 |
| Gas savings, including terminal exit | +1,300 |
| Smaller deduction for unwithdrawn face claims | +1,972,878 |
| **Modeled exit cash advantage** | **+984,800** |
| Lower residual recovery mark | −313,989 |
| **Equity advantage at 50% recovery** | **+670,810** |

The liquidity screen accrued $11.30M of nominal Morpho interest against $10.31M for spread, mostly in msY. How much came back in cash mattered more. Both absorbed about $0.10 of indexed protocol bad debt. The gap came from less capital left in illiquid positions and how those positions were marked. Spread averaged 4.75% in Aave by nominal balance versus 0.12% for liquidity. Its extra Aave interest was $9.3k. Gross turnover was $117.4M versus $154.6M, and the fee savings were $1.3k.

I also reconciled each market by subtracting deposits and fees from withdrawals plus the terminal recovery mark. The two largest contributions sum to **$663k**, or about 99% of the equity gap.

| Market | Liquidity P&L ($k) | Spread P&L ($k) | Contribution to gap ($k) | Gap if omitted from both ($k) |
|---|---:|---:|---:|---:|
| msY, `0x23a7d0ff…` | −1,375 | −996 | +379 | −2 |
| dCOMP, `0x24852d8d…` | −263 | +21 | +284 | −80 |
| AA_FalconXUSDC, `0xe83d72fa…` | +22 | +35 | +13 | +281 |

Both rules exited their initial msY position on 5 June. On 12 June the spread rule ranked msY fifth and filled $1.22M, while the liquidity screen ranked it second and filled $1.62M. The market had zero free USDC at the terminal snapshot. For dCOMP, both rules targeted it on the same nine dates. On 6 November, spread funded about $996k versus $2M for liquidity. Spread fully exited that position on 20 November. The liquidity screen left $575k of face claims after the terminal attempt. These were sizing and funding differences within shared opportunities. The leave-one-out tests reroute capital through the same rules, so their effects can differ from the direct per-market contributions.

## What changed at $4M?

| Prior-day free USDC floor | Liquidity cash / 50% equity ($M) | Spread cash / 50% equity ($M) | Spread equity gap | Shared targets / pooled targets |
|---|---:|---:|---:|---:|
| $2M | 8.033 / 8.561 | 9.018 / 9.232 | +$670,810 | 109 / 149 |
| $4M | 9.637 / 9.925 | 9.637 / 9.925 | −$45 | 60 / 117 |

At $4M all three former msY target dates fail the liquidity gate, as do seven of the nine former dCOMP dates. Both arms end with the same $575k dCOMP residual and the same recovery mark. Spread earns $65.6k more Aave interest and saves $3.4k in gas, offset by $69.0k less Morpho interest. The target overlap falls from 73% to 51% across the 27 decisions. The outcomes converge because the old sources of losses are screened out or reduced, while the remaining carry and fee differences offset.

As a check on the forecast ranking, I kept the spread eligibility, cost hurdle and incumbent holding rules and ranked only eligible new entrants by trailing headline APR. The reference spread ordering added **$328** of terminal equity at 50% recovery versus that diagnostic. This isolates the ordering step. The independent forecasting value of the carry and collateral gates remains open.

## Robustness

| Fixed scenario | Spread minus liquidity at 50% | Spread minus Aave at 50% | At 100% recovery |
|---|---:|---:|---|
| $10M, weekly, $2M floor | +$671k | −$946k | Aave leads both |
| $10M, fortnightly | +$242k | −$1,231k | Liquidity passes spread by $3.5k; Aave leads both |
| $1M, weekly, $200k floor and sleeves | +$349k | −$64k | Spread passes Aave by $11k |
| $10M, weekly, hypothetical $1M Aave withdrawal cap | −$3k | −$227k | Both Morpho arms pass Aave by about $61k–$64k |

The $1M run keeps the same $50 per action and $500k market-supply cutoff. The hypothetical Aave cap leaves $999,950 in modeled exit cash and $9.18M of residual Aave claims marked at par. The benchmark still leads at 50% recovery in every test. Even at $4M, both Morpho arms exceed Aave only under the 100% recovery assumption. Those small reversals depend on the mark.

## Mechanics and conclusion

Rankings use the previous seven daily observations and lagged repayment and collateral data. Opening market snapshots bound execution. I checked msY and dCOMP free USDC against archive blocks 25,297,658 and 26,459,750 for the 12 June and 21 November openings. The indexed values matched to a USDC base unit. Market inclusion uses creation dates and historical supply, so current balances cannot exclude a previously eligible market. Indexer coverage and historical marks remain limitations. Each rebalance conserved cash plus face claims after gas, and no held market-day in the reference lacked rate inputs. All arms apply the same daily Aave compounding and gas charges. Morpho accrues daily with own-deposit dilution and applies indexed bad debt pro rata. Opening liquidity is a capacity proxy, and within-day transaction ordering, withdrawal queues and collateral unwind costs are unmodeled.

I corrected a fully written-off position being treated as an incumbent. It now needs to clear the entry hurdle before it can re-enter. The reference figures were unchanged. I would present this as a **liquidity-screened carry research prototype**. The useful result is how sizing and fill constraints shaped the cash outcome. The gain is concentrated in two markets and fades under tighter screening. Stronger evidence would require a fresh holdout, historical Aave reserve liquidity and executable unwind prices.

Rebuild the attribution and sensitivities offline with `python reports/audit_spread_replay.py`. The [notebook](../notebooks/defi_credit_research.ipynb) shows the underlying decisions and [methodology](methodology.md) documents the model conventions.
