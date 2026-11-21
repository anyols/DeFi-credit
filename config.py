import os
from pathlib import Path


def _load_dotenv(path=".env"):
    path = Path(__file__).parent / path
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

CHAIN_ID = 1  # Ethereum mainnet

RPC_URL = os.environ.get("RPC_URL", "")

# Pinned once an archive RPC was connected (2026-11-21), then frozen for the rest of the
# project so every on-chain read is reproducible. 10 blocks behind mainnet head at pin time.
SNAPSHOT_BLOCK = 26465719

# Verified against docs.morpho.org/get-started/resources/addresses and cross-checked live via
# the Morpho API (market.morphoBlue.address) for every market the chosen vault below allocates to.
MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# Vault choice: searched the Morpho API (GraphQL) for every vault curated by "Keyrock" --
# by name search and by curator address -- across all chains. Exactly one exists:
#   Keyrock USDC (kUSDC), Ethereum mainnet, 0x6C26793c7F1e2785c09b460676e797b716f0Bc8E
#   TVL ~$53.9k as of block 26,465,532; listed=false.
# Two things in SPEC.md do not match live data and should be treated as stale:
#   - The vault address given there, 0x04422053adDbc9bB2759b248B574e3FCA76Bc145, matches no
#     vault the Morpho API knows about on any chain.
#   - "Keyrock Prime USDC", described as the larger sibling vault, does not exist. A similarly
#     named "kpk USDC Prime" vault exists but kpk = karpatkey, a different curator, not Keyrock.
# Of the vault's 4 approved markets (idle USDC, USDC/cbBTC, USDC/WBTC, USDC/sNUSD), only the
# USDC/sNUSD market has supplied assets ($17.3k of a $1M cap); the rest sit idle despite caps
# up to $500M. That is itself a finding worth keeping in the memo, not a reason to pick a
# different vault.
VAULT = "0x6C26793c7F1e2785c09b460676e797b716f0Bc8E"

MORPHO_GRAPHQL_URL = "https://api.morpho.org/graphql"
MORPHO_REST_BASE = "https://api.morpho.org/v0"

# M7 scenario grid (see SPEC.md section 4, M7). Exact per-asset buckets are refined in scenarios.py.
UNDERLYING_SHOCK_RANGE = (-0.60, -0.10, 0.05)  # (min, max, step) on the loan/collateral price ratio
DEPEG_SHOCK_RANGE = (-0.80, -0.02, 0.02)  # for LST/LRT/yield-stable collateral, depeg-only
DEPTH_STRESS_KAPPA = (1, 2, 4)
PATH_STEPS = (1, 6)  # 1 = gap, 6 = gradual
