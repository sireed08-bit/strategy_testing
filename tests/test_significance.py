import random

from strategy_lab.significance import bootstrap_trade_significance


def test_insufficient_trades_is_flagged() -> None:
    result = bootstrap_trade_significance([0.01, 0.02])
    assert result["status"] == "insufficient_trades"


def test_strong_consistent_edge_gets_low_p_value() -> None:
    rng = random.Random(7)
    trades = [0.02 + rng.gauss(0, 0.005) for _ in range(40)]  # ~+2% every trade
    result = bootstrap_trade_significance(trades, rng=random.Random(1))
    assert result["status"] == "evaluated"
    assert result["p_value"] < 0.01
    assert result["ci95_mean_trade_pct"][0] > 0  # CI excludes zero


def test_pure_noise_gets_unremarkable_p_value() -> None:
    rng = random.Random(11)
    trades = [rng.gauss(0, 0.02) for _ in range(40)]  # zero-mean noise
    result = bootstrap_trade_significance(trades, rng=random.Random(2))
    assert result["status"] == "evaluated"
    assert result["p_value"] > 0.05  # cannot reject "no edge"


def test_deterministic_given_rng() -> None:
    trades = [0.01, -0.005, 0.02, 0.003, -0.01, 0.015, 0.007, -0.002]
    a = bootstrap_trade_significance(trades, rng=random.Random(5))
    b = bootstrap_trade_significance(trades, rng=random.Random(5))
    assert a == b
