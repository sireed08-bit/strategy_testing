"""
Indicator library for the market scanner.

Every indicator is a pure function over a list of PriceBar and returns a series
aligned 1:1 with the bars, using None during the warm-up window. Indicators that
need volume or high/low return all-None when those fields are absent (e.g. on
close-only or synthetic data), so callers can simply skip None values.

The INDICATORS registry lets the IC harness evaluate every indicator generically
without knowing their individual signatures.
"""
from __future__ import annotations

from collections.abc import Callable
from statistics import mean, pstdev

from strategy_lab.backtest import PriceBar

Series = list[float | None]


# ── price-only indicators ─────────────────────────────────────────────────────

def rsi_series(bars: list[PriceBar], period: int = 14) -> Series:
    closes = [bar.close for bar in bars]
    out: Series = [None] * len(closes)
    for index in range(period, len(closes)):
        gains = 0.0
        losses = 0.0
        for cursor in range(index - period + 1, index + 1):
            change = closes[cursor] - closes[cursor - 1]
            gains += max(change, 0.0)
            losses += abs(min(change, 0.0))
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0:
            out[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[index] = 100.0 - (100.0 / (1.0 + rs))
    return out


def roc_series(bars: list[PriceBar], period: int = 20) -> Series:
    """Rate of change: percent return over the trailing window."""
    closes = [bar.close for bar in bars]
    out: Series = [None] * len(closes)
    for index in range(period, len(closes)):
        prior = closes[index - period]
        if prior:
            out[index] = (closes[index] / prior - 1.0) * 100.0
    return out


def dist_from_sma_series(bars: list[PriceBar], window: int = 50) -> Series:
    """Percent distance of price above/below its moving average."""
    closes = [bar.close for bar in bars]
    out: Series = [None] * len(closes)
    for index in range(window - 1, len(closes)):
        avg = mean(closes[index - window + 1 : index + 1])
        if avg:
            out[index] = (closes[index] / avg - 1.0) * 100.0
    return out


def bollinger_pctb_series(bars: list[PriceBar], window: int = 20, num_std: float = 2.0) -> Series:
    """%B: position within the Bollinger band (0 = lower band, 1 = upper band)."""
    closes = [bar.close for bar in bars]
    out: Series = [None] * len(closes)
    for index in range(window - 1, len(closes)):
        sample = closes[index - window + 1 : index + 1]
        avg = mean(sample)
        sd = pstdev(sample)
        if sd == 0:
            continue
        lower = avg - num_std * sd
        upper = avg + num_std * sd
        out[index] = (closes[index] - lower) / (upper - lower)
    return out


def macd_histogram_series(
    bars: list[PriceBar], fast: int = 12, slow: int = 26, signal: int = 9
) -> Series:
    """MACD histogram = (EMA_fast - EMA_slow) - signal EMA of that line."""
    closes = [bar.close for bar in bars]
    if len(closes) < slow:
        return [None] * len(closes)
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)
    out: Series = []
    for index in range(len(closes)):
        # Only trust values once the slow EMA has had time to stabilise.
        if index < slow + signal:
            out.append(None)
        else:
            out.append(macd_line[index] - signal_line[index])
    return out


# ── range / OHLC indicators ───────────────────────────────────────────────────

def atr_pct_series(bars: list[PriceBar], period: int = 14) -> Series:
    """Average true range as a percent of price. Needs high/low; else all None."""
    if any(bar.high is None or bar.low is None for bar in bars):
        return [None] * len(bars)
    true_ranges: list[float] = [0.0]
    for index in range(1, len(bars)):
        high = bars[index].high
        low = bars[index].low
        prev_close = bars[index - 1].close
        true_ranges.append(
            max(high - low, abs(high - prev_close), abs(low - prev_close))
        )
    out: Series = [None] * len(bars)
    for index in range(period, len(bars)):
        atr = mean(true_ranges[index - period + 1 : index + 1])
        close = bars[index].close
        if close:
            out[index] = (atr / close) * 100.0
    return out


def gap_pct_series(bars: list[PriceBar]) -> Series:
    """Overnight gap: today's open vs yesterday's close, in percent. Needs open."""
    if any(bar.open is None for bar in bars):
        return [None] * len(bars)
    out: Series = [None]
    for index in range(1, len(bars)):
        prev_close = bars[index - 1].close
        if prev_close:
            out.append((bars[index].open / prev_close - 1.0) * 100.0)
        else:
            out.append(None)
    return out


# ── volume indicators ─────────────────────────────────────────────────────────

def obv_slope_series(bars: list[PriceBar], window: int = 20) -> Series:
    """Normalised slope of On-Balance Volume over the window. Needs volume."""
    if any(bar.volume is None for bar in bars):
        return [None] * len(bars)
    obv = [0.0]
    for index in range(1, len(bars)):
        direction = 0.0
        if bars[index].close > bars[index - 1].close:
            direction = bars[index].volume
        elif bars[index].close < bars[index - 1].close:
            direction = -bars[index].volume
        obv.append(obv[-1] + direction)
    out: Series = [None] * len(bars)
    for index in range(window, len(bars)):
        window_slice = obv[index - window : index + 1]
        scale = max(abs(v) for v in window_slice) or 1.0
        out[index] = (obv[index] - obv[index - window]) / scale
    return out


def volume_zscore_series(bars: list[PriceBar], window: int = 20) -> Series:
    """How unusual today's volume is vs its trailing mean (in std devs)."""
    if any(bar.volume is None for bar in bars):
        return [None] * len(bars)
    volumes = [bar.volume for bar in bars]
    out: Series = [None] * len(bars)
    for index in range(window, len(bars)):
        sample = volumes[index - window : index]
        avg = mean(sample)
        sd = pstdev(sample)
        if sd:
            out[index] = (volumes[index] - avg) / sd
    return out


# ── helpers ───────────────────────────────────────────────────────────────────

def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    ema = [values[0]]
    for value in values[1:]:
        ema.append(alpha * value + (1.0 - alpha) * ema[-1])
    return ema


# ── registry ──────────────────────────────────────────────────────────────────
# name -> callable(bars) -> series. The IC harness iterates this generically.
INDICATORS: dict[str, Callable[[list[PriceBar]], Series]] = {
    "rsi_14": lambda bars: rsi_series(bars, 14),
    "roc_20": lambda bars: roc_series(bars, 20),
    "dist_from_sma_50": lambda bars: dist_from_sma_series(bars, 50),
    "bollinger_pctb_20": lambda bars: bollinger_pctb_series(bars, 20),
    "macd_hist": lambda bars: macd_histogram_series(bars),
    "atr_pct_14": lambda bars: atr_pct_series(bars, 14),
    "gap_pct": gap_pct_series,
    "obv_slope_20": lambda bars: obv_slope_series(bars, 20),
    "volume_zscore_20": lambda bars: volume_zscore_series(bars, 20),
}
