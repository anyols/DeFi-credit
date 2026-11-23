
from kvr import el


def test_el_hand_computed_synthetic_market():
    # Three shock buckets, least severe first. lambda(x) is a simple step function with known
    # values; only -30% carries any loss.
    def lambda_fn(x):
        if x >= 0.30:
            return 0.5
        if x >= 0.20:
            return 1.0
        return 2.0  # x >= 0.10

    total_supply_assets = 1000
    scenario_results = [
        {"market_shock": -0.10, "realized_loss": 0, "economic_loss": 0},
        {"market_shock": -0.20, "realized_loss": 0, "economic_loss": 0},
        {"market_shock": -0.30, "realized_loss": 60, "economic_loss": 40},  # loss_frac = 100/1000 = 0.10
    ]

    # Hand calculation:
    # marginal freq(-0.10) = lambda(0.10) - lambda(0.20) = 2.0 - 1.0 = 1.0
    # marginal freq(-0.20) = lambda(0.20) - lambda(0.30) = 1.0 - 0.5 = 0.5
    # marginal freq(-0.30) = lambda(0.30) - 0            = 0.5 - 0   = 0.5
    # EL = 1.0*0 + 0.5*0 + 0.5*0.10 = 0.05 (fraction) = 500 bps/yr
    expected_bps = 500.0

    got = el.expected_loss_bps(scenario_results, total_supply_assets, lambda_fn)
    assert got == expected_bps, f"expected {expected_bps}, got {got}"


def test_el_marginal_frequencies_hand_computed():
    def lambda_fn(x):
        return {0.10: 2.0, 0.20: 1.0, 0.30: 0.5}[round(x, 2)]

    freqs = el.marginal_frequencies([-0.10, -0.20, -0.30], lambda_fn)
    assert freqs == {-0.10: 1.0, -0.20: 0.5, -0.30: 0.5}


def test_el_returns_none_when_lambda_not_estimable():
    scenario_results = [{"market_shock": -0.10, "realized_loss": 0, "economic_loss": 0}]
    assert el.expected_loss_bps(scenario_results, 1000, None) is None


def test_el_returns_none_when_supply_is_zero():
    scenario_results = [{"market_shock": -0.10, "realized_loss": 0, "economic_loss": 0}]
    assert el.expected_loss_bps(scenario_results, 0, lambda x: 1.0) is None
