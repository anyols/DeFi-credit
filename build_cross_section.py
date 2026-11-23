import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import numpy as np

import config
from kvr import api, chain, scenarios, spread, universe

SHOCKS = scenarios.UNDERLYING_SHOCKS


def main():
    block = chain.w3().eth.block_number
    block_ts = chain.w3().eth.get_block(block)["timestamp"]
    markets = spread.usdc_market_universe(min_supply_usd=1_000_000)
    print(f"{len(markets)} USDC markets with supply > $1M")

    oracle_addrs = [m["oracle"]["address"] for m in markets if m.get("oracle")]
    oracle_classes = spread.batch_classify_oracles(oracle_addrs, block)

    btc_prices = scenarios.fetch_daily_prices("ethereum:0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599")
    lambda_btc = scenarios.exceedance_frequency(btc_prices, horizon_days=1)

    rf_90d = spread.aave_usdc_supply_rate_90d_avg()

    rows = []
    zombie_supply_usd_total = 0.0
    n_zombies = 0
    n_broken_oracle = 0
    for i, m in enumerate(markets):
        symbol = m["collateralAsset"]["symbol"] if m["collateralAsset"] else None
        coll_class = spread.classify_collateral(symbol)
        oracle_addr = m["oracle"]["address"] if m.get("oracle") else None
        oc = oracle_classes.get(oracle_addr, "UNKNOWN") if oracle_addr else "N/A"

        # Same broken-oracle plausibility filter Task 1 already applies to the main universe
        # (universe.select_universe) -- found by hand that this wider, unfiltered pool still
        # includes it: a PAXG market and an sdeUSD market whose oracles both fail this check
        # (reverting or implying an implausible price) report supplyAssetsUsd of $35B and $25B
        # respectively, which would otherwise swamp every other number in this cross-section,
        # including the zombie-supply total below (both also happen to be zombies).
        if oracle_addr and m.get("collateralAsset"):
            implied = universe.implied_collateral_price_usd(oracle_addr, m["collateralAsset"]["decimals"], m["loanAsset"]["decimals"], block)
            if implied is None:
                n_broken_oracle += 1
                print(f"{i+1}/{len(markets)} {symbol}: EXCLUDED (broken oracle, implausible or reverting price)")
                continue

        # Task 2 review point 3: exclude zombie markets (util>=99% for 30d+, no Repay in 30d+)
        # from the cross-section, checked BEFORE the >500%/yr APY filter below so the exclusion
        # is attributed to the actual classifier, not just caught incidentally by that cruder
        # heuristic -- found by hand that every real zombie's quarterlySupplyApy (APY's exp()
        # blowing up for a pinned rate) already exceeds 500%/yr, so checking apy first would
        # silently swallow every zombie into the generic "pinned/incentive-distorted" bucket
        # instead of the principled classifier the review actually asked for.
        zombie = spread.classify_zombie(m["marketId"], before_timestamp=block_ts)
        if zombie["is_zombie"]:
            n_zombies += 1
            zombie_supply_usd_total += m["state"]["supplyAssetsUsd"]
            print(f"{i+1}/{len(markets)} {symbol}: EXCLUDED (zombie, {zombie['days_stuck']}d stuck at cap)")
            continue

        supply_apy = m["state"].get("quarterlySupplyApy")
        if supply_apy is None or supply_apy > 5 or rf_90d is None:  # >500%/yr: pinned/incentive-distorted, exclude
            continue
        spread_bps = (supply_apy - rf_90d) * 10_000

        try:
            illiq = spread.utilization_time_at_cap(m["marketId"])
        except Exception:
            illiq = None

        lambda_fn = lambda_btc if spread.has_tail_history_proxy(coll_class) else None
        el_bps = spread.el_proxy_bps(
            m["state"]["supplyAssetsUsd"], m["state"]["borrowAssetsUsd"], m["state"].get("collateralAssetsUsd"), lambda_fn, SHOCKS
        )

        rows.append(
            {
                "market_id": m["marketId"],
                "collateral_symbol": symbol,
                "collateral_class": coll_class,
                "oracle_class": oc,
                "supply_usd": m["state"]["supplyAssetsUsd"],
                "supply_apy": supply_apy,
                "spread_bps": spread_bps,
                "illiq": illiq,
                "el_bps": el_bps,
            }
        )
        print(f"{i+1}/{len(markets)} {symbol}: oracle={oc} class={coll_class} spread={spread_bps:.1f}bps illiq={illiq} el={el_bps}")
        time.sleep(0.05)

    print(f"\nExcluded {n_broken_oracle} markets with a broken/implausible oracle (not counted toward zombie supply)")
    print(f"Excluded {n_zombies} zombie markets from the cross-section, ${zombie_supply_usd_total:,.0f} total supply")
    Path("data/processed/cross_section.json").write_text(json.dumps(rows, indent=2, default=str))

    full = [r for r in rows if r["illiq"] is not None]
    oracle_dummies = sorted({r["oracle_class"] for r in full})[1:]  # drop one level as baseline
    coll_dummies = sorted({r["collateral_class"] for r in full})[1:]
    X = []
    for r in full:
        row = [r["illiq"]]
        row += [1.0 if r["oracle_class"] == d else 0.0 for d in oracle_dummies]
        row += [1.0 if r["collateral_class"] == d else 0.0 for d in coll_dummies]
        X.append(row)
    names = ["illiq"] + [f"oracle={d}" for d in oracle_dummies] + [f"collateral={d}" for d in coll_dummies]
    full_result = spread.ols_regress([r["spread_bps"] for r in full], X, names)
    print("\n--- Full sample (n=%d): spread ~ illiq + oracle_class + collateral_class ---" % full_result["n"])
    for k, v in full_result["coefficients"].items():
        print(f"  {k}: {v:.2f}")
    print(f"  R^2: {full_result['r_squared']:.3f}")

    with_el = [r for r in full if r["el_bps"] is not None]
    oracle_dummies_el = sorted({r["oracle_class"] for r in with_el})[1:]
    X_el = []
    for r in with_el:
        row = [r["el_bps"], r["illiq"]]
        row += [1.0 if r["oracle_class"] == d else 0.0 for d in oracle_dummies_el]
        X_el.append(row)
    names_el = ["el_bps", "illiq"] + [f"oracle={d}" for d in oracle_dummies_el]
    el_result = spread.ols_regress([r["spread_bps"] for r in with_el], X_el, names_el) if len(with_el) > len(names_el) + 1 else None
    print("\n--- BTC/ETH-LST subsample (n=%d): spread ~ EL + illiq + oracle_class ---" % len(with_el))
    if el_result:
        for k, v in el_result["coefficients"].items():
            print(f"  {k}: {v:.2f}")
        print(f"  R^2: {el_result['r_squared']:.3f}")
    else:
        print("  too few observations for this many parameters")

    Path("data/processed/cross_section_regression.json").write_text(
        json.dumps(
            {
                "full_sample": full_result,
                "btc_eth_subsample": el_result,
                "n_total_universe": len(markets),
                "n_used": len(full),
                "n_zombies_excluded": n_zombies,
                "zombie_supply_usd_total": zombie_supply_usd_total,
                "n_broken_oracle_excluded": n_broken_oracle,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
