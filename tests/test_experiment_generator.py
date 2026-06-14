from strategy_lab.experiment_generator import generate_strategy_variations


def test_generate_strategy_variations_expands_configured_grid() -> None:
    strategies = generate_strategy_variations()

    assert len(strategies) > 20
    assert {strategy.name for strategy in strategies} == {
        "moving_average_cross",
        "rsi_pullback",
    }
    assert all(
        strategy.parameters["fast_sma"] < strategy.parameters["slow_sma"]
        for strategy in strategies
        if strategy.name == "moving_average_cross"
    )

