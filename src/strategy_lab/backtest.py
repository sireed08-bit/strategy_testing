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
    # OHLCV fields are optional so synthetic data and close-only CSVs keep working.
    # When present (real Alpaca data), they unlock true-range ATR, volume, and
    # gap-based indicators used by the market scanner.
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    vwap: float | None = None


# Per-side transaction cost assumption in basis points (5 bps = 0.05%). Applied
# on both entry and exit. Liquid large-cap ETFs trade tighter than this, so it is
# a deliberately conservative haircut to avoid rewarding cost-blind churn.
COST_BPS = 5.0


def run_backtest(
    strategy: StrategySpec,
    bars: list[PriceBar],
    cost_bps: float = COST_BPS,
) -> dict[str, float]:
    if not bars:
        raise ValueError("Backtest needs at least one price bar.")

    ordered = sorted(bars, key=lambda bar: bar.date)
    closes = [bar.close for bar in ordered]
    lows = [bar.low for bar in ordered]
    opens = [bar.open for bar in ordered]
    stop_loss_pct = float(strategy.risk_model.get("stop_loss_pct", 0))
    signals = build_signals_from_bars(strategy, ordered)
    equity_curve, daily_returns, trades = simulate_long_only(
        closes, signals, cost_bps, stop_loss_pct=stop_loss_pct, lows=lows, opens=opens
    )

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


def run_backtest_window(
    strategy: StrategySpec,
    bars: list[PriceBar],
    start_index: int,
    cost_bps: float = COST_BPS,
) -> dict[str, float]:
    """
    Backtest only bars[start_index:], with indicators warmed on the FULL history.

    Running a strategy cold on a short window is not a fair evaluation: an
    SMA-200 filter spends 200 bars warming up and blocks every entry meanwhile,
    so a 30% out-of-sample slice can shrink to a handful of tradable bars for
    exactly the long-lookback combos that most need scrutiny. Here signals are
    computed over the whole series first, then only the window is simulated —
    the position state resets flat at the boundary, but every indicator carries
    its full history into bar one of the window.
    """
    if not bars:
        raise ValueError("Backtest needs at least one price bar.")
    ordered = sorted(bars, key=lambda bar: bar.date)
    if not 0 <= start_index < len(ordered) - 1:
        raise ValueError(f"start_index {start_index} outside usable range for {len(ordered)} bars.")

    signals = build_signals_from_bars(strategy, ordered)
    closes = [bar.close for bar in ordered]
    lows = [bar.low for bar in ordered]
    opens = [bar.open for bar in ordered]
    stop_loss_pct = float(strategy.risk_model.get("stop_loss_pct", 0))

    window_signals = signals[start_index:]
    equity_curve, daily_returns, trades = simulate_long_only(
        closes[start_index:],
        window_signals,
        cost_bps,
        stop_loss_pct=stop_loss_pct,
        lows=lows[start_index:],
        opens=opens[start_index:],
    )
    return {
        "annualized_return_pct": annualized_return_pct(equity_curve),
        "max_drawdown_pct": max_drawdown_pct(equity_curve),
        "sharpe_ratio": sharpe_ratio(daily_returns),
        "sortino_ratio": sortino_ratio(daily_returns),
        "profit_factor": profit_factor(trades),
        "trade_count": float(len(trades)),
        "win_rate_pct": win_rate_pct(trades),
        "exposure_pct": exposure_pct(window_signals),
        "regime_consistency": chunk_consistency(daily_returns),
        "robustness_score": chunk_consistency(daily_returns),
    }


# Strategy families that need OHLCV beyond close (gap, true range, volume).
_OHLCV_FAMILIES = {"sma_reversion", "gap_momentum"}


def build_signals_from_bars(strategy: StrategySpec, bars: list[PriceBar]) -> list[bool]:
    """
    Bars-aware signal dispatcher. OHLCV-based families (derived from the scanner's
    top Information-Coefficient indicators) are computed here; everything else
    falls back to the close-only build_signals so existing strategies are untouched.
    """
    closes = [bar.close for bar in bars]

    if strategy.name == "sma_reversion":
        # From dist_from_sma_50 (the cleanest IC: price far below its SMA reverts).
        window = int(strategy.parameters["sma_window"])
        entry_pct = float(strategy.parameters["entry_pct"])
        exit_pct = float(strategy.parameters["exit_pct"])
        max_hold = int(strategy.risk_model.get("max_hold_days", 0))
        signals: list[bool] = []
        in_position = False
        days_held = 0
        for index in range(len(closes)):
            if index + 1 < window:
                signals.append(False)
                continue
            avg = sma(closes, index, window)
            if in_position:
                days_held += 1
                reverted = closes[index] >= avg * (1.0 - exit_pct / 100.0)
                forced = max_hold > 0 and days_held >= max_hold
                if reverted or forced:
                    in_position = False
                    days_held = 0
            elif closes[index] <= avg * (1.0 - entry_pct / 100.0):
                in_position = True
                days_held = 0
            signals.append(in_position)
        return signals

    if strategy.name == "gap_momentum":
        # From gap_pct (positive IC: up-gaps show short-term continuation).
        opens = [bar.open for bar in bars]
        if any(o is None for o in opens):
            return [False] * len(closes)  # needs open prices
        gap_threshold = float(strategy.parameters["gap_pct"])
        max_hold = int(strategy.risk_model.get("max_hold_days", 3))
        signals = []
        in_position = False
        days_held = 0
        for index in range(len(closes)):
            if index == 0:
                signals.append(False)
                continue
            if in_position:
                days_held += 1
                if max_hold > 0 and days_held >= max_hold:
                    in_position = False
                    days_held = 0
            else:
                gap = (opens[index] - closes[index - 1]) / closes[index - 1] * 100.0
                if gap >= gap_threshold:
                    in_position = True
                    days_held = 0
            signals.append(in_position)
        return signals

    return build_signals(strategy, closes)


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
        sma_filter = int(strategy.parameters.get("sma_filter", 0))
        max_hold_days = int(strategy.risk_model.get("max_hold_days", 0))
        rsi_signals: list[bool] = []
        in_position = False
        days_held = 0
        for index in range(len(closes)):
            value = rsi(closes, index, period)
            if value is None:
                rsi_signals.append(False)
                continue
            if in_position:
                days_held += 1
                forced_exit = max_hold_days > 0 and days_held >= max_hold_days
                if value >= exit_rsi or forced_exit:
                    in_position = False
                    days_held = 0
            else:
                above_trend = sma_filter == 0 or closes[index] > sma(closes, index, sma_filter)
                if value <= entry_rsi and above_trend:
                    in_position = True
                    days_held = 0
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
    cost_bps: float = 0.0,
    stop_loss_pct: float = 0.0,
    lows: list[float | None] | None = None,
    opens: list[float | None] | None = None,
) -> tuple[list[float], list[float], list[float]]:
    """
    Long-only equity simulation with realistic execution.

    - T+1 entry: a signal decided on bar t takes effect on bar t+1, so the bar
      that defines a signal is never also the bar it is acted on (no look-ahead).
    - Transaction cost: cost_bps basis points are deducted on each entry and exit,
      folded into both the equity curve and daily returns so every metric is net.
    - Stop loss: when stop_loss_pct > 0, a position is force-exited once price
      falls that far below its entry. The exit is booked at a realistic fill:
      the stop price itself, or the open when the bar gapped straight through it
      — never the close, which would credit intraday recoveries a real stop order
      would have missed. After a stop-out the simulation stays flat until the
      strategy's own signal resets, so a stopped trade is not re-entered next bar.
    """
    cost = cost_bps / 10_000.0
    # Shift signals forward one bar to enforce T+1 execution.
    effective = ([False] + signals[:-1]) if signals else []
    stop_fills: dict[int, float] = {}
    if stop_loss_pct > 0:
        effective, stop_fills = _apply_stop_loss(
            effective, closes, stop_loss_pct / 100.0, lows, opens
        )

    equity = 1.0
    equity_curve = [equity]
    daily_returns: list[float] = []
    trades: list[float] = []
    entry_price: float | None = None

    for index in range(1, len(closes)):
        was_in_position = effective[index - 1]
        is_in_position = effective[index]

        # A stop-out bar exits at the stop fill price, not the close.
        exit_price = stop_fills.get(index, closes[index])

        bar_return = 0.0
        if was_in_position:
            price_out = exit_price if (not is_in_position and index in stop_fills) else closes[index]
            bar_return = price_out / closes[index - 1] - 1.0

        if not was_in_position and is_in_position:
            entry_price = closes[index]
            bar_return -= cost  # pay entry cost on the bar we enter

        if was_in_position and not is_in_position and entry_price is not None:
            gross = exit_price / entry_price - 1.0
            trades.append((1.0 + gross) * (1.0 - cost) ** 2 - 1.0)  # round-trip net
            bar_return -= cost  # pay exit cost on the bar we exit
            entry_price = None

        equity *= 1.0 + bar_return
        daily_returns.append(bar_return)
        equity_curve.append(equity)

    if effective and effective[-1] and entry_price is not None:
        gross = closes[-1] / entry_price - 1.0
        trades.append((1.0 + gross) * (1.0 - cost) ** 2 - 1.0)

    return equity_curve, daily_returns, trades


def _apply_stop_loss(
    effective: list[bool],
    closes: list[float],
    stop: float,
    lows: list[float | None] | None,
    opens: list[float | None] | None = None,
) -> tuple[list[bool], dict[int, float]]:
    """
    Force-exit a position once price falls `stop` (fraction) below its entry.

    Returns the adjusted signal series plus {bar_index: fill_price} for each
    stop-out. The fill is the stop price when the bar traded through it, or the
    open when the bar gapped below the stop before trading (you cannot fill
    better than the open). The stop is tested against the bar low when available
    (a realistic intraday trigger), otherwise the close — and with close-only
    data the fill is the close itself, since that is the only known price.

    After a stop-out the position stays flat until the underlying signal resets
    (goes flat, then fires again) so a stopped trade is not re-entered on the
    very next bar.
    """
    result = list(effective)
    fills: dict[int, float] = {}
    entry_price: float | None = None
    suppressed = False
    for index in range(len(result)):
        if not result[index]:
            suppressed = False  # strategy flat → clear suppression, no open trade
            entry_price = None
            continue
        if suppressed:
            result[index] = False  # stopped out earlier; remain flat
            continue
        if entry_price is None:
            entry_price = closes[index]  # entry bar — no stop check yet
            continue
        stop_price = entry_price * (1.0 - stop)
        low_known = lows is not None and lows[index] is not None
        low = lows[index] if low_known else closes[index]
        if low <= stop_price:
            result[index] = False  # stop hit → exit this bar
            if not low_known:
                fill = closes[index]  # close-only data: close is the only price
            else:
                bar_open = opens[index] if (opens is not None and opens[index] is not None) else None
                if bar_open is not None and bar_open < stop_price:
                    fill = bar_open  # gapped through the stop → filled at the open
                else:
                    fill = stop_price
            fills[index] = fill
            suppressed = True
            entry_price = None
    return result, fills


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

