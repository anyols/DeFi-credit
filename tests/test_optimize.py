
import pytest

from kvr import optimize

CBBTC = {"supply_assets": 270657976376041, "borrow_assets": 177926270185516, "rate_at_target": 1546947627, "cap": 99986073000000}
WBTC = {"supply_assets": 156334608098001, "borrow_assets": 102167566517944, "rate_at_target": 1552011577, "cap": 99986073000000}
SNUSD = {"supply_assets": 18239784318, "borrow_assets": 18239778780, "rate_at_target": 63419583967}


def test_slsqp_matches_greedy_on_well_behaved_markets():
    result = optimize.slsqp_check([CBBTC, WBTC], budget=1_000_000 * 10**6)
    assert result is not None
    assert result["rel_diff"] < 0.01, f"greedy={result['greedy_objective']} slsqp={result['slsqp_objective']}"


def test_snusd_income_rate_spikes_near_zero_then_settles():
    """Documents the known artifact directly: income_rate(x) is non-monotonic and extreme near
    x=0 for a market whose rate is already pinned at the IRM's ceiling, then becomes sane.
    """
    tiny = optimize.income_rate(1_000 * 10**6, SNUSD["supply_assets"], SNUSD["borrow_assets"], SNUSD["rate_at_target"])
    settled = optimize.income_rate(309_149 * 10**6, SNUSD["supply_assets"], SNUSD["borrow_assets"], SNUSD["rate_at_target"])
    implied_apy_tiny = tiny / (1_000 * 10**6)
    implied_apy_settled = settled / (309_149 * 10**6)
    assert implied_apy_tiny > 100  # >10,000% implied APY at a $1,000 deposit
    assert implied_apy_settled < 0.10  # back to a sane <10% by $309k
