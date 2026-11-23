import pytest

import config
from kvr import liq_edge, spread, stream_case


def test_stream_historical_borrowers_are_cut_off_and_shares_reconciled(monkeypatch):
    events = [{"blockNumber": 99, "user": {"address": "0x" + "01" * 20}},
              {"blockNumber": 101, "user": {"address": "0x" + "02" * 20}}]
    reads = []

    def paginate(_query, variables, _path):
        assert variables["end"] == 1000
        return events

    def read(address, fn, args, block):
        assert address == config.MORPHO_BLUE and block == 100
        reads.append(fn)
        if fn == "market":
            return 0, 0, 0, 25, 0, 0
        assert args[1].lower() == events[0]["user"]["address"]
        return 0, 25, 0

    monkeypatch.setattr(stream_case.api, "_paginate", paginate)
    monkeypatch.setattr(stream_case, "arb_call", read)
    assert stream_case.historical_concentration(stream_case.XUSD_MARKET_ID, 100, 1000)["hhi"] == 1
    assert reads == ["market", "position"]
    events.clear()
    with pytest.raises(ValueError, match="reconcile"):
        stream_case.historical_concentration(stream_case.XUSD_MARKET_ID, 100, 1000)


def test_zombie_series_excludes_post_cutoff_observations(monkeypatch):
    def graphql(_query, variables):
        assert variables["end"] == 172800
        points = [{"x": x, "y": y} for x, y in [(86400, .999), (172800, .999), (259200, .5)]]
        return {"marketById": {"historicalState": {"utilization": points}}}

    monkeypatch.setattr(spread.api, "graphql", graphql)
    assert spread.consecutive_days_at_utilization_cap("M", end_timestamp=172800) == (2, True)


def test_oracle_marked_pnl_and_missing_gas(monkeypatch):
    event = {"blockNumber": 42, "timestamp": 100, "txHash": "0x1",
             "data": {"seizedAssets": 1_100_000_000, "repaidAssets": 1_000_000_000, "badDebtAssets": 42}}
    monkeypatch.setattr(liq_edge.oracle, "read_price", lambda *a, **kw: 10**36)
    monkeypatch.setattr(liq_edge, "gas_cost_usd", lambda h, b: 20 if b == 42 else None)
    row = liq_edge.event_economics(event, "oracle", "WBTC", "BTC", .04)
    assert row["oracle_implied_bonus_usd"] == 100
    assert row["profit_usd"] == pytest.approx(100 - 20 - 1100 * config.LIQ_EDGE_HEDGE_COST_BPS / 10_000)
    assert row["execution_pnl_usd"] is None
    monkeypatch.setattr(liq_edge, "gas_cost_usd", lambda *_: None)
    row = liq_edge.event_economics(event, "oracle", "WBTC", "BTC", .04)
    assert row["profit_usd"] is None
    summary = liq_edge.summarize_market([event], [None], [])
    assert summary["n_insolvent"] == 1 and summary["total_left_on_table_usd"] == 42 / 1e6
    assert summary["profitable_share_volume"] is None
