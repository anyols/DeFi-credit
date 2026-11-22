"""Fetch dated inputs for the historical replay, or replay a cached snapshot offline."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

import config
from kvr import api, backtest, chain, historical_replay as replay, stream_case

CACHE = Path("data/raw/historical_replay.json")
OUTPUT = Path("data/processed/historical_replay.json")
FIELDS = ("supplyAssets", "borrowAssets", "rateAtTarget", "collateralAssetsUsd")


def discover_markets():
    query = """
    query($loan: [String!], $first: Int!, $skip: Int!) {
      markets(first: $first, skip: $skip, orderBy: UniqueKey, orderDirection: Asc,
              where: {chainId_in: [1], loanAssetAddress_in: $loan, isIdle: false}) {
        pageInfo { countTotal }
        items { marketId creationBlockNumber creationTimestamp collateralAsset { symbol } }
      }
    }
    """
    rows = api._paginate(query, {"loan": [backtest.USDC]}, "markets")
    if len({row["marketId"] for row in rows}) != len(rows):
        raise RuntimeError("Duplicate markets in historical universe")
    return {
        row["marketId"]: {"symbol": (row["collateralAsset"] or {}).get("symbol"),
                          "created": int(row["creationTimestamp"])}
        for row in rows if int(row["creationBlockNumber"]) <= config.SNAPSHOT_BLOCK
    }


def fetch_history(mid, created, start, end):
    fields = "\n".join(
        f"{field}(options: {{startTimestamp:$start,endTimestamp:$end,interval:DAY}}) {{x y}}"
        for field in FIELDS
    )
    query = f"""
    query($id:String!, $start:Int!, $end:Int!) {{
      marketById(marketId:$id,chainId:1) {{ historicalState {{ {fields} }} }}
    }}"""
    data = api.graphql(query, {"id": mid, "start": max(start, backtest._day(created)), "end": end})
    history = {}
    for field, points in data["marketById"]["historicalState"].items():
        for p in points or []:
            t = int(p["x"])
            if p["y"] is None or t < created or t % backtest.DAY or t > end:
                continue
            value = float(p["y"]) if field == "collateralAssetsUsd" else int(p["y"])
            history.setdefault(t, {})[field] = value
    return history


def fetch_events(mid, start, end):
    # Need only positive Repay occurrences. A date is flagged once regardless of trade count.
    repayments = api.get_market_transfers_chunked(mid, "Repay", start, end)
    return (sorted({backtest._day(x["timestamp"]) for x in repayments if int(x["data"]["assets"]) > 0}),
            backtest.pull_bad_debt_by_day(mid, start, end))


def collect():
    end = backtest._day(chain.w3().eth.get_block(config.SNAPSHOT_BLOCK)["timestamp"])
    start = end - config.BACKTEST_LOOKBACK_DAYS * backtest.DAY
    warmup = start - replay.SCREEN_DAYS * backtest.DAY
    markets = {mid: m for mid, m in discover_markets().items() if m["created"] < end}
    print(f"Fetching historical state for {len(markets)} markets...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        series = pool.map(lambda item: fetch_history(item[0], item[1]["created"], warmup, end), markets.items())
        histories = {mid: hist for mid, hist in zip(markets, series)
                     if any(d < end and r.get("supplyAssets", 0) >= replay.MIN_SUPPLY * 1e6
                            for d, r in hist.items())}
        print(f"Fetching event histories for {len(histories)} markets...", flush=True)
        events = dict(zip(histories, pool.map(lambda mid: fetch_events(mid, warmup, end - 1), histories)))
    return {
        "start": start, "end": end, "snapshot_block": config.SNAPSHOT_BLOCK,
        "discovered": len(markets), "markets": {mid: markets[mid] for mid in histories},
        "histories": histories, "events": events,
        "aave": {d: v for d, v in backtest.aave_daily_history().items() if start <= d < end},
    }


def spot_check(data, example):
    """Compare indexed free USDC to stored market balances near the replay timestamps."""
    if example is None:
        return []
    mid = example["market_id"]
    observed = next(int(t) for t in data["histories"][mid] if replay.date(int(t)) == example["latest_input_date"])
    checks = []
    w3 = chain.w3()
    for day in (observed, data["end"]):
        block = stream_case.find_block_by_timestamp(day, w3=w3)
        supply, _, borrow, *_ = chain.call_morpho("market", [bytes.fromhex(mid[2:])], block)
        onchain = max(0, supply - borrow) / replay.UNIT
        indexed = replay.liquidity(data["histories"][mid].get(str(day)))
        checks.append({"date": replay.date(day), "block": block, "market_id": mid,
                       "indexed_free_usdc": indexed, "onchain_free_usdc": onchain,
                       "absolute_difference_usdc": abs(indexed - onchain)})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Refresh the historical input cache")
    parser.add_argument("--spot-check", action="store_true", help="Check worked-example liquidity against RPC")
    args = parser.parse_args()
    if args.refresh or not CACHE.exists():
        data = collect()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache_temp = CACHE.with_suffix(".tmp")
        cache_temp.write_text(json.dumps(data), encoding="utf-8")
        cache_temp.replace(CACHE)
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    result = replay.run(data)
    if args.spot_check:
        result["onchain_liquidity_spot_checks"] = {
            key: spot_check(data, example) for key, example in result["worked_decisions"].items()
        }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(result["table"], indent=2))


if __name__ == "__main__":
    main()
