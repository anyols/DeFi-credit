"""Package the pinned, credential-free input files for the Streamlit demo."""
import gzip
import json
import os
from pathlib import Path

import pandas as pd

from build_report import load_market
from kvr import chain


def main():
    os.chdir(Path(__file__).resolve().parent)
    processed = Path("data/processed")
    replay = json.loads(Path("data/raw/historical_replay.json").read_text(encoding="utf-8"))
    block = replay["snapshot_block"]
    timestamp = chain.w3().eth.get_block(block)["timestamp"]
    report = json.loads((processed / "report_results.json").read_text(encoding="utf-8"))
    markets = pd.read_parquet(processed / "markets.parquet")
    inputs = {}
    for row in markets.itertuples():
        label = f"USDC/{row.collateral_symbol}"
        fit = report["slippage_fits"].get(label, {})
        if not fit.get("fit"):
            continue
        market = load_market(row.market_id, markets, block, timestamp)
        inputs[label] = {
            key: market[key] for key in ("market_id", "price", "lltv", "borrow_assets", "borrow_shares", "supply_assets")
        }
        inputs[label].update(
            oracle_class=report["oracle_class"][label],
            fit=fit["fit"],
            fit_source=fit["source"],
            positions=[{"user": i, "borrow_shares": p["borrow_shares"], "collateral": p["collateral"]}
                       for i, p in enumerate(market["positions"])],
        )
    for filename, data in (
        ("demo_replay.json.gz", replay), ("demo_stress.json.gz", {"block": block, "markets": inputs})
    ):
        (processed / filename).write_bytes(
            gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(), mtime=0)
        )


if __name__ == "__main__":
    main()
