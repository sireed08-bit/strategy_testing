from strategy_lab.experiment_generator import generate_strategy_variations

ALL_STRATEGY_NAMES = {
    "moving_average_cross",
    "rsi_pullback",
    "donchian_breakout",
    "volatility_contraction_expansion",
    "spy_tlt_regime_switch",
    "relative_strength_rotation",
    "sector_momentum_leadership",
    "sma_reversion",
    "gap_momentum",
}


def test_generate_strategy_variations_expands_configured_grid() -> None:
    strategies = generate_strategy_variations()

    assert len(strategies) > 20
    assert {strategy.name for strategy in strategies} == ALL_STRATEGY_NAMES
    assert all(
        strategy.parameters["fast_sma"] < strategy.parameters["slow_sma"]
        for strategy in strategies
        if strategy.name == "moving_average_cross"
    )
    assert all(
        strategy.parameters["exit_lookback"] < strategy.parameters["entry_lookback"]
        for strategy in strategies
        if strategy.name == "donchian_breakout"
    )
