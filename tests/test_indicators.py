import math

from strategy_lab.backtest import PriceBar
from strategy_lab.indicators import (
    INDICATORS,
    atr_pct_series,
    rsi_series,
    volume_zscore_series,
)
from strategy_lab.indicator_eval import (
    evaluate_all_indicators,
    forward_return_series,
    spearman,
)


def _ohlcv_bars(n: int = 300, symbol: str = "SPY") -> list[PriceBar]:
    bars = []
    price = 100.0
    for day in range(n):
        price = price * (1.0 + 0.0004) + 5.0 * math.sin(day * 0.2)
        close = max(1.0, price)
        bars.append(
            PriceBar(
                date=f"2021-{(day // 28) % 12 + 1:02d}-{day % 28 + 1:02d}",
                symbol=symbol,
                close=close,
                open=close * 0.999,
                high=close * 1.01,
                low=close * 0.99,
                volume=1_000_000 + (day % 10) * 50_000,
                vwap=close,
            )
        )
    return bars


def test_rsi_series_bounded_and_warms_up() -> None:
    bars = _ohlcv_bars()
    series = rsi_series(bars, period=14)
    assert series[:14] == [None] * 14
    values = [v for v in series if v is not None]
    assert values and all(0.0 <= v <= 100.0 for v in values)


def test_atr_pct_returns_none_without_high_low() -> None:
    close_only = [PriceBar(date=f"2021-01-{d:02d}", symbol="SPY", close=100.0 + d) for d in range(1, 30)]
    assert all(v is None for v in atr_pct_series(close_only))
    # With OHLC data it produces values.
    assert any(v is not None for v in atr_pct_series(_ohlcv_bars()))


def test_volume_zscore_flags_unusual_volume() -> None:
    bars = _ohlcv_bars(120)
    # Spike the last bar's volume far above its trailing mean.
    spiked = bars[:-1] + [
        PriceBar(
            date=bars[-1].date,
            symbol="SPY",
            close=bars[-1].close,
            open=bars[-1].open,
            high=bars[-1].high,
            low=bars[-1].low,
            volume=50_000_000,
            vwap=bars[-1].vwap,
        )
    ]
    z = volume_zscore_series(spiked, window=20)
    assert z[-1] is not None and z[-1] > 3.0


def test_spearman_perfect_monotonic_is_one() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert abs(spearman(xs, ys) - 1.0) < 1e-9


def test_forward_return_series_alignment() -> None:
    bars = [PriceBar(date=f"2021-01-{d:02d}", symbol="SPY", close=float(100 + d)) for d in range(1, 11)]
    fwd = forward_return_series(bars, horizon=2)
    assert fwd[-1] is None and fwd[-2] is None  # no future bars to look ahead to
    assert fwd[0] is not None


def test_evaluate_all_indicators_ranks_by_abs_ic() -> None:
    bars_by_symbol = {"SPY": _ohlcv_bars(), "QQQ": _ohlcv_bars(symbol="QQQ")}
    results = evaluate_all_indicators(bars_by_symbol)
    assert len(results) == len(INDICATORS)
    evaluated = [r for r in results if r.get("status") == "evaluated"]
    assert evaluated  # at least some indicators produced an IC
    abs_ics = [r["abs_ic"] for r in evaluated]
    assert abs_ics == sorted(abs_ics, reverse=True)  # ranked strongest-first
