from datetime import date, timedelta

from strategy_lab.backtest import PriceBar, run_backtest
from strategy_lab.models import StrategySpec
from strategy_lab.strategy_ideas import seed_strategy_specs

EXPECTED_METRIC_KEYS = {
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


def _trending_bars(days: int = 400, symbol: str = "SPY") -> list[PriceBar]:
    start = date(2022, 1, 1)
    return [
        PriceBar(
            date=(start + timedelta(days=day)).isoformat(),
            symbol=symbol,
            close=100.0 + day * 0.4,
        )
        for day in range(days)
    ]


def _volatile_bars(days: int = 400, symbol: str = "SPY") -> list[PriceBar]:
    """Bars with a sine-wave oscillation around a gentle uptrend."""
    import math

    start = date(2022, 1, 1)
    return [
        PriceBar(
            date=(start + timedelta(days=day)).isoformat(),
            symbol=symbol,
            close=100.0 + day * 0.1 + 10.0 * math.sin(day * 0.2),
        )
        for day in range(days)
    ]


def test_run_backtest_returns_scoring_metrics_for_moving_average_strategy() -> None:
    strategy = seed_strategy_specs()[0]
    metrics = run_backtest(strategy, _trending_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS
    assert metrics["exposure_pct"] > 0


def test_run_backtest_rejects_unimplemented_strategy() -> None:
    strategy = StrategySpec(
        family="test",
        name="nonexistent_strategy_xyz",
        hypothesis="",
        rules={},
        parameters={},
    )
    bars = _trending_bars(days=30)
    try:
        run_backtest(strategy, bars)
    except ValueError as exc:
        assert "No v1 backtest implementation" in str(exc)
    else:
        raise AssertionError("Expected an unimplemented strategy error.")


def test_run_backtest_rsi_pullback_returns_all_metrics() -> None:
    strategy = seed_strategy_specs()[1]  # rsi_pullback
    metrics = run_backtest(strategy, _volatile_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS


def test_run_backtest_donchian_breakout_returns_all_metrics() -> None:
    strategy = StrategySpec(
        family="breakout",
        name="donchian_breakout",
        hypothesis="New highs after consolidation.",
        rules={"entry": "close above channel high", "exit": "close below channel low"},
        parameters={"entry_lookback": 20, "exit_lookback": 10},
        risk_model={"position_size_pct": 20, "stop_loss_pct": 8},
    )
    metrics = run_backtest(strategy, _trending_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS


def test_run_backtest_volatility_contraction_expansion_returns_all_metrics() -> None:
    strategy = StrategySpec(
        family="volatility",
        name="volatility_contraction_expansion",
        hypothesis="Breakouts after vol contraction.",
        rules={"entry": "vol contracts then price breaks range", "exit": "price below range low"},
        parameters={"contraction_days": 10, "breakout_days": 5, "atr_period": 10},
        risk_model={"position_size_pct": 15},
    )
    metrics = run_backtest(strategy, _volatile_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS


def test_run_backtest_spy_tlt_regime_switch_returns_all_metrics() -> None:
    strategy = StrategySpec(
        family="risk_on_risk_off",
        name="spy_tlt_regime_switch",
        hypothesis="Price above SMA = risk on.",
        rules={"entry": "price above trend_sma", "exit": "price below trend_sma"},
        parameters={"trend_sma": 100},
        risk_model={"position_size_pct": 100},
    )
    metrics = run_backtest(strategy, _trending_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS


def test_run_backtest_relative_strength_rotation_returns_all_metrics() -> None:
    strategy = StrategySpec(
        family="momentum",
        name="relative_strength_rotation",
        hypothesis="Positive trailing momentum continues.",
        rules={"entry": "trailing return positive", "exit": "trailing return negative"},
        parameters={"lookback_days": 63, "rebalance_days": 21},
        risk_model={"position_size_pct": 50},
    )
    metrics = run_backtest(strategy, _trending_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS


def test_run_backtest_sector_momentum_leadership_returns_all_metrics() -> None:
    strategy = StrategySpec(
        family="sector_rotation",
        name="sector_momentum_leadership",
        hypothesis="Positive risk-adjusted trailing return continues.",
        rules={"entry": "risk-adj return positive", "exit": "risk-adj return negative"},
        parameters={"lookback_days": 63, "rebalance_days": 21},
        risk_model={"position_size_pct": 33},
    )
    metrics = run_backtest(strategy, _trending_bars())
    assert set(metrics) == EXPECTED_METRIC_KEYS
