from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean, pstdev

from strategy_lab.models import StrategySpec


@dataclass(frozen=True)
class PriceBar:
    date: str
    symbol: str
    close: float


def run_backtest(strategy: StrategySpec, bars: list[PriceBar]) -> dict[str, float]:
    if not bars:
        raise ValueError("Backtest needs at least one price bar.")

    ordered = sorted(bars, key=lambda bar: bar.date)
    closes = [bar.close for bar in ordered]
    signals = build_signals(strategy, closes)
    equity_curve, daily_returns, trades = simulate_long_only(closes, signals)

    return {
        "annualized_return_pct": annualized_return_pct(equity_curve),
        "max_drawdown_pct": max_drawdown_pct(equity_curve),
        "sharpe_ratio": sharpe_ratio(daily_returns),
        "sortino_ratio": sortino_ratio(daily_returns),
        "profit_factor": profit_factor(trades),
        "trade_count": float(len(trades)),
        "win_rate_pct": win_rate_pct(trades),
        "exposure_pct": exposure_pct(signals),
        "regime_consistency": chunk_consistency(daily_returns),
        "robustness_score": chunk_consistency(daily_returns),
    }


def build_signals(strategy: StrategySpec, closes: list[float]) -> list[bool]:
    if strategy.name == "moving_average_cross":
        fast = int(strategy.parameters["fast_sma"])
        slow = int(strategy.parameters["slow_sma"])
        return [
            index >= slow and sma(closes, index, fast) > sma(closes, index, slow)
            for index in range(len(closes))
        ]

    if strategy.name == "rsi_pullback":
        period = int(strategy.parameters["rsi_period"])
        entry_rsi = float(strategy.parameters["entry_rsi"])
        exit_rsi = float(strategy.parameters["exit_rsi"])
        rsi_signals: list[bool] = []
        in_position = False
        for index in range(len(closes)):
            value = rsi(closes, index, period)
            if value is None:
                rsi_signals.append(False)
                continue
            if not in_position and value <= entry_rsi:
                in_position = True
            elif in_position and value >= exit_rsi:
                in_position = False
            rsi_signals.append(in_position)
        return rsi_signals

    if strategy.name == "donchian_breakout":
        entry_n = int(strategy.parameters["entry_lookback"])
        exit_n = int(strategy.parameters["exit_lookback"])
        donchian_signals: list[bool] = []
        in_position = False
        for index in range(len(closes)):
            if index < entry_n:
                donchian_signals.append(False)
                continue
            channel_high = max(closes[index - entry_n : index])
            channel_low = min(closes[index - exit_n : index])
            if not in_position and closes[index] > channel_high:
                in_position = True
            elif in_position and closes[index] < channel_low:
                in_position = False
            donchian_signals.append(in_position)
        return donchian_signals

    if strategy.name == "volatility_contraction_expansion":
        contraction_days = int(strategy.parameters["contraction_days"])
        breakout_days = int(strategy.parameters["breakout_days"])
        atr_period = int(strategy.parameters["atr_period"])
        required = contraction_days + atr_period
        vol_signals: list[bool] = []
        in_position = False
        for index in range(len(closes)):
            if index < required:
                vol_signals.append(False)
                continue
            recent_atr = atr_closes(closes, index, atr_period)
            prior_atr = atr_closes(closes, index - contraction_days, atr_period)
            lo = max(0, index - breakout_days)
            range_high = max(closes[lo:index])
            range_low = min(closes[lo:index])
            contracted = prior_atr > 0 and recent_atr < prior_atr * 0.8
            if not in_position and contracted and closes[index] > range_high:
                in_position = True
            elif in_position and closes[index] < range_low:
                in_position = False
            vol_signals.append(in_position)
        return vol_signals

    if strategy.name == "spy_tlt_regime_switch":
        trend_period = int(strategy.parameters["trend_sma"])
        return [
            index >= trend_period and closes[index] > sma(closes, index, trend_period)
            for index in range(len(closes))
        ]

    if strategy.name == "relative_strength_rotation":
        lookback = int(strategy.parameters["lookback_days"])
        rebalance = int(strategy.parameters["rebalance_days"])
        rs_signals: list[bool] = []
        in_position = False
        for index in range(len(closes)):
            if index < lookback:
                rs_signals.append(False)
                continue
            if (index - lookback) % rebalance == 0:
                trailing_return = closes[index] / closes[index - lookback] - 1.0
                in_position = trailing_return > 0
            rs_signals.append(in_position)
        return rs_signals

    if strategy.name == "sector_momentum_leadership":
        lookback = int(strategy.parameters["lookback_days"])
        rebalance = int(strategy.parameters["rebalance_days"])
        sec_signals: list[bool] = []
        in_position = False
        for index in range(len(closes)):
            if index < lookback:
                sec_signals.append(False)
                continue
            if (index - lookback) % rebalance == 0:
                window = closes[index - lookback : index + 1]
                ret = window[-1] / window[0] - 1.0
                daily_rets = [window[j] / window[j - 1] - 1.0 for j in range(1, len(window))]
                vol = pstdev(daily_rets) if len(daily_rets) > 1 else 0.0
                risk_adj = ret / vol if vol > 0 else 0.0
                in_position = risk_adj > 0
            sec_signals.append(in_position)
        return sec_signals

    raise ValueError(f"No v1 backtest implementation for strategy: {strategy.name}")


def simulate_long_only(
    closes: list[float],
    signals: list[bool],
) -> tuple[list[float], list[float], list[float]]:
    equity = 1.0
    equity_curve = [equity]
    daily_returns: list[float] = []
    trades: list[float] = []
    entry_price: float | None = None

    for index in range(1, len(closes)):
        was_in_position = signals[index - 1]
        is_in_position = signals[index]

        if not was_in_position and is_in_position:
            entry_price = closes[index]

        period_return = 0.0
        if was_in_position:
            period_return = closes[index] / closes[index - 1] - 1.0
            equity *= 1.0 + period_return

        if was_in_position and not is_in_position and entry_price is not None:
            trades.append(closes[index] / entry_price - 1.0)
            entry_price = None

        daily_returns.append(period_return)
        equity_curve.append(equity)

    if signals[-1] and entry_price is not None:
        trades.append(closes[-1] / entry_price - 1.0)

    return equity_curve, daily_returns, trades


def sma(values: list[float], index: int, window: int) -> float:
    if index + 1 < window:
        return values[index]
    sample = values[index + 1 - window : index + 1]
    return sum(sample) / window


def rsi(values: list[float], index: int, period: int) -> float | None:
    if index < period:
        return None
    gains = []
    losses = []
    for cursor in range(index - period + 1, index + 1):
        change = values[cursor] - values[cursor - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def atr_closes(closes: list[float], index: int, period: int) -> float:
    if index < period:
        return 0.0
    changes = [abs(closes[i] - closes[i - 1]) for i in range(index - period + 1, index + 1)]
    return sum(changes) / period


def annualized_return_pct(equity_curve: list[float]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    total_return = equity_curve[-1] / equity_curve[0]
    annualized = total_return ** (252 / (len(equity_curve) - 1)) - 1.0
    return round(annualized * 100.0, 2)


def max_drawdown_pct(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = value / peak - 1.0
        max_drawdown = min(max_drawdown, drawdown)
    return round(abs(max_drawdown) * 100.0, 2)


def sharpe_ratio(returns: list[float]) -> float:
    if len(returns) < 2 or pstdev(returns) == 0:
        return 0.0
    return round((mean(returns) / pstdev(returns)) * sqrt(252), 2)


def sortino_ratio(returns: list[float]) -> float:
    downside = [value for value in returns if value < 0]
    if len(downside) < 2 or pstdev(downside) == 0:
        return 0.0
    return round((mean(returns) / pstdev(downside)) * sqrt(252), 2)


def profit_factor(trades: list[float]) -> float:
    gross_profit = sum(value for value in trades if value > 0)
    gross_loss = abs(sum(value for value in trades if value < 0))
    if gross_loss == 0:
        return round(gross_profit / 0.0001, 2) if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 2)


def win_rate_pct(trades: list[float]) -> float:
    if not trades:
        return 0.0
    wins = len([value for value in trades if value > 0])
    return round((wins / len(trades)) * 100.0, 2)


def exposure_pct(signals: list[bool]) -> float:
    if not signals:
        return 0.0
    return round((len([signal for signal in signals if signal]) / len(signals)) * 100.0, 2)


def chunk_consistency(returns: list[float], chunks: int = 4) -> float:
    if not returns:
        return 0.0
    chunk_size = max(1, len(returns) // chunks)
    chunk_returns = [
        sum(returns[index : index + chunk_size])
        for index in range(0, len(returns), chunk_size)
    ]
    positive_chunks = len([value for value in chunk_returns if value > 0])
    return round(positive_chunks / len(chunk_returns), 2)

