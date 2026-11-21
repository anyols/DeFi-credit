"""One command reproduces everything. Currently covers M1 (universe) and M2 (positions);
later stages are added as their modules land (see SPEC.md section 7).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import config
from kvr import universe, positions


def main():
    if not config.RPC_URL:
        sys.exit("RPC_URL is not set. Copy .env.example to .env and fill in an archive RPC URL.")
    if not config.SNAPSHOT_BLOCK:
        sys.exit("config.SNAPSHOT_BLOCK is not pinned yet. Set it once an RPC key is connected.")

    print(f"Snapshot block {config.SNAPSHOT_BLOCK}, vault {config.VAULT}")

    markets = universe.build_markets()
    universe.save(markets)
    print(f"M1: {len(markets)} markets -> data/processed/markets.parquet")

    for market_id in markets["market_id"]:
        pos = positions.build_positions(market_id)
        path = positions.save(pos, market_id)
        print(f"M2: {market_id[:10]}: {len(pos)} positions -> {path}")


if __name__ == "__main__":
    main()
