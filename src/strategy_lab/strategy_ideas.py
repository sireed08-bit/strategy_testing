from __future__ import annotations

from strategy_lab.fingerprints import experiment_fingerprint
from strategy_lab.models import DatasetSpec, ExperimentRecord, StrategySpec
from strategy_lab.scoring import score_metrics


DEFAULT_DATASET = DatasetSpec(
    name="daily_us_equities_research_sample",
    symbols=["SPY", "QQQ", "IWM", "DIA"],
    timeframe="1D",
    start="2018-01-01",
    end="2025-12-31",
)


def seed_strategy_specs() -> list[StrategySpec]:
    return [
        StrategySpec(
            family="trend_following",
            name="moving_average_cross",
            hypothesis="Medium-term trend alignment can capture persistent equity moves.",
            rules={
                "entry": "fast_sma crosses above slow_sma",
                "exit": "fast_sma crosses below slow_sma",
            },
            parameters={"fast_sma": 50, "slow_sma": 200},
            risk_model={"position_size_pct": 25, "stop_loss_pct": 12},
        ),
        StrategySpec(
            family="mean_reversion",
            name="rsi_pullback",
            hypothesis="Oversold pullbacks in liquid index ETFs may mean-revert.",
            rules={
                "entry": "rsi below threshold and price above long_sma",
                "exit": "rsi recovers or max_hold_days reached",
            },
            parameters={"rsi_period": 14, "entry_rsi": 30, "exit_rsi": 50},
            risk_model={"position_size_pct": 20, "max_hold_days": 10},
        ),
        StrategySpec(
            family="breakout",
            name="donchian_breakout",
            hypothesis="New highs after consolidation may continue in the breakout direction.",
            rules={
                "entry": "close above lookback high",
                "exit": "close below trailing channel low",
            },
            parameters={"entry_lookback": 55, "exit_lookback": 20},
            risk_model={"position_size_pct": 20, "atr_stop_multiple": 3},
        ),
        StrategySpec(
            family="momentum",
            name="relative_strength_rotation",
            hypothesis="Assets with stronger intermediate momentum may continue to lead.",
            rules={
                "entry": "rank symbols by trailing return and hold top bucket",
                "exit": "drop below rebalance rank threshold",
            },
            parameters={"lookback_days": 126, "hold_count": 2, "rebalance_days": 21},
            risk_model={"position_size_pct": 50, "max_symbol_weight_pct": 50},
        ),
        StrategySpec(
            family="volatility",
            name="volatility_contraction_expansion",
            hypothesis="Breakouts following volatility contraction may offer better reward/risk.",
            rules={
                "entry": "realized volatility contracts then price closes above range",
                "exit": "atr trailing stop or failed breakout",
            },
            parameters={"contraction_days": 20, "breakout_days": 10, "atr_period": 14},
            risk_model={"position_size_pct": 20, "atr_stop_multiple": 2.5},
        ),
        StrategySpec(
            family="sector_rotation",
            name="sector_momentum_leadership",
            hypothesis="Sector leadership persistence can improve broad-market exposure timing.",
            rules={
                "entry": "hold strongest sector ETFs by trailing risk-adjusted return",
                "exit": "sector falls below leadership threshold",
            },
            parameters={"lookback_days": 90, "hold_count": 3, "rebalance_days": 21},
            risk_model={"position_size_pct": 33, "max_sector_weight_pct": 34},
        ),
        StrategySpec(
            family="risk_on_risk_off",
            name="spy_tlt_regime_switch",
            hypothesis="Simple risk regime filters may reduce equity drawdowns.",
            rules={
                "risk_on": "SPY above 200-day SMA",
                "risk_off": "allocate to TLT or cash proxy",
            },
            parameters={"trend_sma": 200, "risk_off_asset": "TLT"},
            risk_model={"position_size_pct": 100},
        ),
    ]


def placeholder_records(dataset: DatasetSpec = DEFAULT_DATASET) -> list[ExperimentRecord]:
    records: list[ExperimentRecord] = []
    empty_metrics = {
        "annualized_return_pct": 0,
        "max_drawdown_pct": 0,
        "sharpe_ratio": 0,
        "sortino_ratio": 0,
        "profit_factor": 0,
        "trade_count": 0,
        "win_rate_pct": 0,
        "exposure_pct": 0,
        "regime_consistency": 0,
        "robustness_score": 0,
    }

    for spec in seed_strategy_specs():
        result = score_metrics(empty_metrics)
        records.append(
            ExperimentRecord(
                strategy=spec,
                dataset=dataset,
                metrics=empty_metrics,
                score=result.score,
                grade="watch",
                conclusion="Seed idea only; backtest metrics not yet attached.",
                fingerprint=experiment_fingerprint(spec, dataset),
                weaknesses=["not backtested"],
                next_action="Run initial backtest and replace placeholder metrics.",
            )
        )
    return records
