from datetime import date, timedelta

from strategy_lab.backtest import PriceBar, build_signals, run_backtest, simulate_long_only
from strategy_lab.models import StrategySpec
from strategy_lab.strategy_ideas import seed_strategy_specs

EXPECTED_METRIC_KEYS = {
    "annualized_return_pct",
    "benchmark_return_pct",
    "excess_return_pct",
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


def test_simulate_long_only_enters_on_the_bar_after_signal() -> None:
    # Signal is true on bars 1-3. With T+1 execution the position is held over
    # the steps following each true signal, never the step that defines it.
    closes = [100.0, 110.0, 121.0, 133.1, 120.0]
    signals = [False, True, True, True, False]
    equity_curve, daily_returns, trades = simulate_long_only(closes, signals, cost_bps=0.0)
    # Return on the step from bar1->bar2 must be zero: the position only turns on
    # at bar 2 (one bar after the signal first fired at bar 1).
    assert daily_returns[0] == 0.0  # bar0->bar1
    assert daily_returns[1] == 0.0  # bar1->bar2, position not yet effective
    assert daily_returns[2] != 0.0  # bar2->bar3, now in position


def test_simulate_long_only_transaction_cost_reduces_returns() -> None:
    closes = [100.0, 105.0, 103.0, 108.0, 110.0, 107.0]
    signals = [False, True, True, False, True, True]
    free_curve, _, _ = simulate_long_only(closes, signals, cost_bps=0.0)
    costed_curve, _, _ = simulate_long_only(closes, signals, cost_bps=10.0)
    # Any round trip should leave the cost-bearing run with strictly less equity.
    assert costed_curve[-1] < free_curve[-1]


def test_stop_loss_caps_loss_and_blocks_reentry() -> None:
    # Enter at bar 1 (signal true from bar 0), then price craters. A 10% stop must
    # force an exit and the trade loss must be near the stop, not the full crash.
    closes = [100.0, 100.0, 95.0, 85.0, 70.0, 60.0, 60.0]
    signals = [True, True, True, True, True, True, True]  # always "in" per the strategy
    _, _, trades_no_stop = simulate_long_only(closes, signals, cost_bps=0.0)
    _, _, trades_stop = simulate_long_only(closes, signals, cost_bps=0.0, stop_loss_pct=10.0)
    # Without a stop the single open trade rides the full decline.
    assert min(trades_no_stop) < -0.30
    # With a 10% stop the realised loss is capped far tighter.
    assert min(trades_stop) > -0.25


def test_stop_loss_uses_intraday_low_when_available() -> None:
    # Close never breaches the stop, but an intraday low does — the stop should fire.
    closes = [100.0, 100.0, 100.0, 100.0]
    lows = [100.0, 100.0, 88.0, 100.0]  # bar 2 dipped to 88 (>10% below 100 entry)
    signals = [True, True, True, True]
    _, _, trades = simulate_long_only(closes, signals, cost_bps=0.0, stop_loss_pct=10.0, lows=lows)
    assert len(trades) >= 1  # a stop-out trade was recorded


def test_stop_loss_fills_at_stop_price_not_recovered_close() -> None:
    # Bar 2 dips through the 10% stop intraday (low=88) but closes back at 100.
    # A real stop order fills at ~90; booking the recovered close of 100 would
    # credit the trade with upside a stopped-out position never had.
    closes = [100.0, 100.0, 100.0, 100.0]
    lows = [100.0, 100.0, 88.0, 100.0]
    signals = [True, True, True, True]
    _, _, trades = simulate_long_only(closes, signals, cost_bps=0.0, stop_loss_pct=10.0, lows=lows)
    assert len(trades) == 1
    assert abs(trades[0] - (-0.10)) < 1e-9  # exit at exactly the stop price


def test_stop_loss_fills_at_open_when_bar_gaps_through_stop() -> None:
    # Bar 2 opens at 80, far below the 90 stop — the fill cannot be better than
    # the open. Booking the stop price (90) would understate the gap loss.
    closes = [100.0, 100.0, 82.0, 82.0]
    lows = [100.0, 100.0, 78.0, 82.0]
    opens = [100.0, 100.0, 80.0, 82.0]
    signals = [True, True, True, True]
    _, _, trades = simulate_long_only(
        closes, signals, cost_bps=0.0, stop_loss_pct=10.0, lows=lows, opens=opens
    )
    assert len(trades) == 1
    assert abs(trades[0] - (-0.20)) < 1e-9  # filled at the 80 open, not the 90 stop


def test_excess_return_is_negative_for_a_flat_strategy_in_a_bull_market() -> None:
    """A strategy that never trades on a rising symbol must show excess return
    of exactly minus the benchmark: doing nothing has an opportunity cost."""
    bars = _trending_bars(days=300)
    strategy = StrategySpec(
        family="mean_reversion",
        name="rsi_pullback",
        hypothesis="",
        rules={},
        # entry_rsi=1 on a monotonic uptrend: RSI stays pinned high, never enters.
        parameters={"rsi_period": 14, "entry_rsi": 1, "exit_rsi": 99, "sma_filter": 0},
        risk_model={},
    )
    metrics = run_backtest(strategy, bars)
    assert metrics["trade_count"] == 0.0
    assert metrics["benchmark_return_pct"] > 0
    assert metrics["excess_return_pct"] == round(0.0 - metrics["benchmark_return_pct"], 2)


def test_scoring_weights_sum_to_one_and_include_excess_return() -> None:
    from strategy_lab.scoring import load_criteria

    criteria = load_criteria()
    assert "excess_return_pct" in criteria["weights"]
    assert abs(sum(criteria["weights"].values()) - 1.0) < 1e-9
    assert "excess_return_pct" in criteria["metric_targets"]


def test_profit_target_fills_at_target_not_higher_close() -> None:
    # Flat tape (ATR from close changes = 1.0 via lows/highs), entry at 100.
    # Target = 100 + 3*ATR. Bar 17's high touches the target intraday but the
    # close finishes above it — a resting limit order fills AT the target.
    closes = [100.0 + (i % 2) for i in range(16)] + [100.0, 106.0]
    highs = [c + 0.5 for c in closes[:-1]] + [107.0]
    lows = [c - 0.5 for c in closes]
    signals = [True] * len(closes)
    _, _, trades = simulate_long_only(
        closes, signals, cost_bps=0.0, profit_target_atr=3.0, highs=highs, lows=lows
    )
    assert len(trades) == 1
    # Entry at closes[1]; ATR(14 at entry) derives from the alternating tape.
    # The trade's net return must correspond to a fill BELOW the 106 close.
    assert 0 < trades[0] < (106.0 / closes[1] - 1.0)


def test_profit_target_absent_is_a_noop() -> None:
    closes = [100.0, 101.0, 99.0, 102.0, 104.0, 103.0]
    signals = [True] * len(closes)
    plain = simulate_long_only(closes, signals, cost_bps=5.0)
    with_zero = simulate_long_only(closes, signals, cost_bps=5.0, profit_target_atr=0.0)
    assert plain == with_zero


def test_stop_beats_target_on_the_same_bar() -> None:
    # Bar 16 has a huge range: low breaches the stop AND high touches the target.
    # Conservative same-bar priority: the stop must win.
    closes = [100.0 + (i % 2) for i in range(16)] + [100.0]
    highs = [c + 0.5 for c in closes[:-1]] + [130.0]
    lows = [c - 0.5 for c in closes[:-1]] + [80.0]
    signals = [True] * len(closes)
    _, _, trades = simulate_long_only(
        closes, signals, cost_bps=0.0,
        stop_loss_pct=10.0, profit_target_atr=3.0, highs=highs, lows=lows,
    )
    assert len(trades) == 1
    assert trades[0] < 0  # stopped out, not target-filled


def test_vol_target_zero_is_an_exact_noop() -> None:
    """Absent/zero vol_target must reproduce the unsized simulation bit-for-bit
    — existing fingerprints and results stay valid without a rebuild."""
    closes = [100.0, 104.0, 99.0, 106.0, 103.0, 108.0, 101.0, 110.0]
    signals = [True] * len(closes)
    plain = simulate_long_only(closes, signals, cost_bps=5.0)
    zeroed = simulate_long_only(closes, signals, cost_bps=5.0, vol_target_pct=0.0)
    assert plain == zeroed


def test_vol_target_reduces_drawdown_on_a_volatile_series() -> None:
    import math

    # Calm first half, violent second half — vol targeting should de-risk into
    # the storm and cut the drawdown versus the fully-invested run.
    closes = []
    price = 100.0
    for day in range(120):
        swing = 0.002 if day < 60 else 0.045  # calm, then violent
        price = max(1.0, price * (1.0 + swing * math.sin(day * 1.3)))
        closes.append(price)
    signals = [True] * len(closes)
    unsized, _, _ = simulate_long_only(closes, signals, cost_bps=0.0)
    sized, _, _ = simulate_long_only(closes, signals, cost_bps=0.0, vol_target_pct=10.0)

    from strategy_lab.backtest import max_drawdown_pct

    assert max_drawdown_pct(sized) < max_drawdown_pct(unsized)


def test_yearly_breakdown_splits_strategy_and_benchmark_by_year() -> None:
    from strategy_lab.backtest import yearly_breakdown

    # Two calendar years of trending bars; an always-long strategy must roughly
    # match the benchmark within each year (net of the tiny cost drag).
    bars = _trending_bars(days=500)  # 2022-01-01 .. mid-2023
    strategy = StrategySpec(
        family="risk_on_risk_off",
        name="spy_tlt_regime_switch",
        hypothesis="",
        rules={},
        parameters={"trend_sma": 10},  # short SMA on a monotonic uptrend = ~always long
        risk_model={},
    )
    rows = yearly_breakdown(strategy, bars, cost_bps=0.0)
    assert [row["year"] for row in rows] == ["2022", "2023"]
    assert all(row["benchmark_pct"] > 0 for row in rows)  # uptrend both years
    # 2022 carries the warmup drag: the strategy sits out the first ~11 bars of
    # a compounding rally, so its excess is negative — and attributed to the
    # RIGHT year, which is exactly what this breakdown exists to show.
    assert -20.0 < rows[0]["excess_pct"] < 0.0
    # 2023 has no warmup: fully long all year, excess ~0 at zero cost.
    assert abs(rows[1]["excess_pct"]) < 1.0


def test_bollinger_reversion_enters_on_snap_back_not_falling_knife() -> None:
    from strategy_lab.backtest import build_signals

    # Price crashes below the lower band and STAYS below — no entry while
    # falling. Only the close that crosses back above the lower band enters.
    closes = [100.0] * 20 + [80.0, 78.0, 76.0, 88.0, 92.0]
    spec = StrategySpec(
        family="mean_reversion", name="bollinger_reversion", hypothesis="", rules={},
        parameters={"window": 20, "num_std": 2.0}, risk_model={},
    )
    signals = build_signals(spec, closes)
    assert signals[20] is False and signals[21] is False and signals[22] is False
    assert signals[23] is True  # 88 closes back above the (crash-widened) lower band


def test_percent_rank_momentum_enters_at_high_rank() -> None:
    from strategy_lab.backtest import build_signals

    closes = [float(100 + i) for i in range(20)] + [125.0, 90.0]
    spec = StrategySpec(
        family="momentum", name="percent_rank_momentum", hypothesis="", rules={},
        parameters={"lookback": 20, "entry_percentile": 80, "exit_percentile": 30},
        risk_model={},
    )
    signals = build_signals(spec, closes)
    assert signals[20] is True   # 125 ranks above 100% of the trailing window
    assert signals[21] is False  # 90 ranks at 0% → exit


def test_day_of_week_momentum_only_enters_on_configured_weekday() -> None:
    from strategy_lab.backtest import build_signals_from_bars
    from datetime import date, timedelta

    start = date(2026, 1, 5)  # a Monday
    bars = []
    for day in range(300):
        d = start + timedelta(days=day)
        if d.weekday() > 4:
            continue  # trading days only
        bars.append(PriceBar(date=d.isoformat(), symbol="QQQ", close=100.0 + len(bars) * 0.5))
    spec = StrategySpec(
        family="calendar", name="day_of_week_momentum", hypothesis="", rules={},
        parameters={"weekday": 3, "momentum_lookback": 100},  # Thursdays
        risk_model={"max_hold_days": 2},
    )
    signals = build_signals_from_bars(spec, bars)
    # Every ENTRY bar (False -> True transition) must be a Thursday.
    for i in range(1, len(bars)):
        if signals[i] and not signals[i - 1]:
            assert date.fromisoformat(bars[i].date).weekday() == 3


def test_turn_of_month_holds_only_around_month_boundaries() -> None:
    from strategy_lab.backtest import build_signals_from_bars

    bars = [PriceBar(date=f"2026-03-{d:02d}", symbol="SPY", close=100.0) for d in range(1, 32)]
    spec = StrategySpec(
        family="calendar", name="turn_of_month", hypothesis="", rules={},
        parameters={"entry_day": 26, "exit_day": 3}, risk_model={},
    )
    signals = build_signals_from_bars(spec, bars)
    by_day = {d + 1: signals[d] for d in range(31)}
    assert by_day[2] is True and by_day[3] is True     # first days of the month
    assert by_day[15] is False and by_day[20] is False  # mid-month flat
    assert by_day[26] is True and by_day[31] is True    # month-end run-up


def test_month_range_hold_wraps_year_boundary() -> None:
    from strategy_lab.backtest import build_signals_from_bars

    bars = [PriceBar(date=f"2026-{m:02d}-15", symbol="SPY", close=100.0) for m in range(1, 13)]
    spec = StrategySpec(
        family="calendar", name="month_range_hold", hypothesis="", rules={},
        parameters={"start_month": 11, "end_month": 4}, risk_model={},
    )
    signals = build_signals_from_bars(spec, bars)
    held_months = [m + 1 for m in range(12) if signals[m]]
    assert held_months == [1, 2, 3, 4, 11, 12]  # Nov-Apr, wrapped


def test_dual_momentum_band_requires_all_three_conditions() -> None:
    from strategy_lab.backtest import build_signals

    # Monotonic uptrend: long-term momentum is UP, so the long_down condition
    # fails everywhere — never a single entry.
    closes = [100.0 + i for i in range(260)]
    spec = StrategySpec(
        family="mean_reversion", name="dual_momentum_band", hypothesis="", rules={},
        parameters={"long_lookback": 200, "fast_lookback": 10, "slow_lookback": 40},
        risk_model={"max_hold_days": 21},
    )
    assert not any(build_signals(spec, closes))


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


def test_run_backtest_rsi_pullback_sma_filter_never_increases_trades() -> None:
    bars = _volatile_bars()
    base = StrategySpec(
        family="mean_reversion",
        name="rsi_pullback",
        hypothesis="RSI pullback, no filter.",
        rules={},
        parameters={"rsi_period": 14, "entry_rsi": 35, "exit_rsi": 55, "sma_filter": 0},
        risk_model={"position_size_pct": 15, "max_hold_days": 5},
    )
    filtered = StrategySpec(
        family="mean_reversion",
        name="rsi_pullback",
        hypothesis="RSI pullback with 200-day SMA trend filter.",
        rules={},
        parameters={"rsi_period": 14, "entry_rsi": 35, "exit_rsi": 55, "sma_filter": 200},
        risk_model={"position_size_pct": 15, "max_hold_days": 5},
    )
    base_metrics = run_backtest(base, bars)
    filtered_metrics = run_backtest(filtered, bars)
    assert set(filtered_metrics) == EXPECTED_METRIC_KEYS
    assert filtered_metrics["trade_count"] <= base_metrics["trade_count"]


def test_rsi_pullback_max_hold_days_caps_holding_period() -> None:
    """A max_hold_days cap must force exits no later than N bars after entry."""
    closes = [100.0 - day for day in range(40)]  # steady decline: RSI stays oversold, never recovers

    def hold_run_lengths(max_hold_days: int) -> list[int]:
        spec = StrategySpec(
            family="mean_reversion",
            name="rsi_pullback",
            hypothesis="",
            rules={},
            parameters={"rsi_period": 5, "entry_rsi": 40, "exit_rsi": 60, "sma_filter": 0},
            risk_model={"max_hold_days": max_hold_days},
        )
        signals = build_signals(spec, closes)
        runs: list[int] = []
        current = 0
        for flag in signals:
            if flag:
                current += 1
            elif current:
                runs.append(current)
                current = 0
        if current:
            runs.append(current)
        return runs

    # With no exit_rsi recovery possible, an uncapped run holds for many bars...
    assert max(hold_run_lengths(max_hold_days=0)) > 5
    # ...but a 5-day cap means no single position is ever held longer than 5 bars.
    assert max(hold_run_lengths(max_hold_days=5)) <= 5


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
