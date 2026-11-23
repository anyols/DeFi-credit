# DeFi Credit Lab

Can higher USDC lending yields compensate for the risk of getting stuck? I studied Morpho Blue liquidations and replayed three lending rules against Aave.

[Live Streamlit dashboard](https://defi-credit-jcu4fhmf7sve4etltytzul.streamlit.app/) · [Research notebook](notebooks/defi_credit_research.ipynb) · [Methodology](reports/methodology.md)

## Key findings

- **Aave led the 18-month replay.** On $10M starting capital, Aave ended at $10.55M assuming full exit at par. The best Morpho rule produced $2.63M in modeled exit cash and $3.90M at 50% recovery on collateral-covered claims.
- **Reported balances can overstate what lenders can access.** Five illiquid markets showed $1.22B in supply claims and about 161 USDC in immediate withdrawal liquidity at the snapshot.
- **Liquidation incentives depend on the exit.** In the WBTC, cbBTC and wstETH markets, 97.4% to 99.6% of priced non-dust repayment volume had positive modeled P&L. In RLP, Morpho recorded $1.18M in bad debt.

In a separate six-month evaluation, a liquidity-adjusted spread rule improved modeled exit cash over the liquidity screen. It still trailed Aave on recovery-adjusted equity. [Read the strategy audit](reports/strategy_validation.md).

## Run locally

Use Python 3.11 from the repository root. The app and notebook use bundled data and require no API keys.

```sh
python -m pip install -r requirements-notebook.txt
python -m streamlit run streamlit_app.py
jupyter execute --timeout=120 notebooks/defi_credit_research.ipynb
```
