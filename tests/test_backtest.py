from datetime import date, timedelta

from strategy_lab.backtest import PriceBar, run_backtest
from strategy_lab.strategy_ideas import seed_strategy_specs


def test_run_backtest_returns_scoring_metrics_for_moving_average_strategy() -> None:
    strategy = seed_strategy_specs()[0]
    start = date(2025, 1, 1)
    bars = [
        PriceBar(
            date=(start + timedelta(days=day)).isoformat(),
            symbol="SPY",
            close=100.0 + day * 0.5,
        )
        for day in range(260)
    ]

    metrics = run_backtest(strategy, bars)

    assert set(metrics) == {
        "annualized_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "sortino_ratio",
        "profit_factor",
        "trade_count",
        "win_rate_pct",
        "exposure_pct",
        "regime_consistency",
        "robustness_score",
    }
    assert metrics["exposure_pct"] > 0


def test_run_backtest_rejects_unimplemented_strategy() -> None:
    strategy = seed_strategy_specs()[2]
    start = date(2025, 1, 1)
    bars = [
        PriceBar(
            date=(start + timedelta(days=day)).isoformat(),
            symbol="SPY",
            close=100.0 + day,
        )
        for day in range(30)
    ]

    try:
        run_backtest(strategy, bars)
    except ValueError as exc:
        assert "No v1 backtest implementation" in str(exc)
    else:
        raise AssertionError("Expected an unimplemented strategy error.")
