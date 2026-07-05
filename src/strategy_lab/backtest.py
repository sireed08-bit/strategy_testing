from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
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
    highs = [bar.high for bar in ordered]
    signals = build_signals_from_bars(strategy, ordered)
    equity_curve, daily_returns, trades = simulate_long_only(
        closes, signals, cost_bps,
        stop_loss_pct=float(strategy.risk_model.get("stop_loss_pct", 0)),
        lows=lows, opens=opens, highs=highs,
        vol_target_pct=float(strategy.risk_model.get("vol_target_pct", 0)),
        profit_target_atr=float(strategy.risk_model.get("profit_target_atr", 0)),
    )
    return assemble_metrics(equity_curve, daily_returns, trades, signals, closes)


def assemble_metrics(
    equity_curve: list[float],
    daily_returns: list[float],
    trades: list[float],
    signals: list[bool],
    closes: list[float],
) -> dict[str, float]:
    """
    Metric set shared by full-history and windowed backtests.

    Includes the benchmark comparison: a strategy is only worth anything if it
    beats (or risk-adjusts better than) simply holding the same symbol over the
    same bars. The close series IS the buy-and-hold equity curve, so the same
    annualisation applies to both. excess_return_pct is the strategy's edge over
    doing nothing — the number this whole system exists to maximise.
    """
    strategy_return = annualized_return_pct(equity_curve)
    benchmark_return = annualized_return_pct(closes)
    return {
        "annualized_return_pct": strategy_return,
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": round(strategy_return - benchmark_return, 2),
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


def yearly_breakdown(
    strategy: StrategySpec,
    bars: list[PriceBar],
    cost_bps: float = COST_BPS,
) -> list[dict]:
    """
    Strategy vs buy-and-hold return per calendar year.

    Aggregate metrics hide regime dependence: a strategy that earned everything
    in one rebound year and nothing since shows the same annualised return as
    one that earned steadily. This is the first question to ask of any
    multi-year result.
    """
    if len(bars) < 2:
        return []
    ordered = sorted(bars, key=lambda bar: bar.date)
    closes = [bar.close for bar in ordered]
    signals = build_signals_from_bars(strategy, ordered)
    _, daily_returns, _ = simulate_long_only(
        closes,
        signals,
        cost_bps,
        stop_loss_pct=float(strategy.risk_model.get("stop_loss_pct", 0)),
        lows=[bar.low for bar in ordered],
        opens=[bar.open for bar in ordered],
        highs=[bar.high for bar in ordered],
        vol_target_pct=float(strategy.risk_model.get("vol_target_pct", 0)),
        profit_target_atr=float(strategy.risk_model.get("profit_target_atr", 0)),
    )

    # daily_returns[i-1] covers the move from bars[i-1] to bars[i]; attribute it
    # to the year of the bar it lands on.
    by_year: dict[str, dict] = {}
    for index in range(1, len(ordered)):
        year = ordered[index].date[:4]
        slot = by_year.setdefault(
            year, {"strategy_growth": 1.0, "benchmark_growth": 1.0}
        )
        slot["strategy_growth"] *= 1.0 + daily_returns[index - 1]
        slot["benchmark_growth"] *= closes[index] / closes[index - 1]

    rows = []
    for year in sorted(by_year):
        strat = (by_year[year]["strategy_growth"] - 1.0) * 100.0
        bench = (by_year[year]["benchmark_growth"] - 1.0) * 100.0
        rows.append(
            {
                "year": year,
                "strategy_pct": round(strat, 2),
                "benchmark_pct": round(bench, 2),
                "excess_pct": round(strat - bench, 2),
            }
        )
    return rows


def backtest_trades(
    strategy: StrategySpec,
    bars: list[PriceBar],
    cost_bps: float = COST_BPS,
) -> list[float]:
    """Just the per-trade net returns — used by significance testing."""
    if not bars:
        return []
    ordered = sorted(bars, key=lambda bar: bar.date)
    closes = [bar.close for bar in ordered]
    signals = build_signals_from_bars(strategy, ordered)
    _, _, trades = simulate_long_only(
        closes,
        signals,
        cost_bps,
        stop_loss_pct=float(strategy.risk_model.get("stop_loss_pct", 0)),
        lows=[bar.low for bar in ordered],
        opens=[bar.open for bar in ordered],
        highs=[bar.high for bar in ordered],
        profit_target_atr=float(strategy.risk_model.get("profit_target_atr", 0)),
    )
    return trades


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
    window_closes = closes[start_index:]
    equity_curve, daily_returns, trades = simulate_long_only(
        window_closes,
        window_signals,
        cost_bps,
        stop_loss_pct=stop_loss_pct,
        lows=lows[start_index:],
        opens=opens[start_index:],
        highs=[bar.high for bar in ordered][start_index:],
        vol_target_pct=float(strategy.risk_model.get("vol_target_pct", 0)),
        profit_target_atr=float(strategy.risk_model.get("profit_target_atr", 0)),
    )
    return assemble_metrics(equity_curve, daily_returns, trades, window_signals, window_closes)


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

    if strategy.name == "day_of_week_momentum":
        # From the YT mined-strategy catalog (Kevin Davey's NQ day-of-week
        # momentum): enter on a specific weekday only when long momentum is up.
        # Original exits were opposite-signal + catastrophic stop; the daily
        # translation exits on max_hold or when the momentum condition dies.
        weekday = int(strategy.parameters["weekday"])  # 0=Mon .. 4=Fri
        lookback = int(strategy.parameters["momentum_lookback"])
        max_hold = int(strategy.risk_model.get("max_hold_days", 5))
        dow_signals: list[bool] = []
        in_position = False
        days_held = 0
        for index in range(len(bars)):
            if index < lookback:
                dow_signals.append(False)
                continue
            momentum_up = closes[index] > closes[index - lookback]
            if in_position:
                days_held += 1
                if (max_hold > 0 and days_held >= max_hold) or not momentum_up:
                    in_position = False
                    days_held = 0
            elif momentum_up and _date.fromisoformat(bars[index].date).weekday() == weekday:
                in_position = True
                days_held = 0
            dow_signals.append(in_position)
        return dow_signals

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

    if strategy.name == "bollinger_reversion":
        # YT catalog (Davey, long-only translation): enter when the close
        # crosses back ABOVE the lower Bollinger band (was below, now above —
        # the snap-back, not the falling knife); exit at the upper band.
        window = int(strategy.parameters["window"])
        num_std = float(strategy.parameters["num_std"])
        max_hold = int(strategy.risk_model.get("max_hold_days", 0))
        bb_signals: list[bool] = []
        in_position = False
        days_held = 0
        prev_below_lower = False
        for index in range(len(closes)):
            if index + 1 < window:
                bb_signals.append(False)
                continue
            sample = closes[index + 1 - window : index + 1]
            mid = sum(sample) / window
            sd = pstdev(sample)
            lower = mid - num_std * sd
            upper = mid + num_std * sd
            if in_position:
                days_held += 1
                forced = max_hold > 0 and days_held >= max_hold
                if closes[index] >= upper or forced:
                    in_position = False
                    days_held = 0
            elif prev_below_lower and closes[index] > lower:
                in_position = True
                days_held = 0
            prev_below_lower = closes[index] < lower
            bb_signals.append(in_position)
        return bb_signals

    if strategy.name == "percent_rank_momentum":
        # YT catalog ("Rolling Window Strength Bias", ChatGPT via Davey): long
        # when today's close ranks above entry_percentile of the trailing
        # window; exit when it sinks below exit_percentile.
        lookback = int(strategy.parameters["lookback"])
        entry_pctl = float(strategy.parameters["entry_percentile"])
        exit_pctl = float(strategy.parameters["exit_percentile"])
        max_hold = int(strategy.risk_model.get("max_hold_days", 0))
        pr_signals: list[bool] = []
        in_position = False
        days_held = 0
        for index in range(len(closes)):
            if index < lookback:
                pr_signals.append(False)
                continue
            past = closes[index - lookback : index]
            rank_pct = 100.0 * sum(1 for value in past if value < closes[index]) / lookback
            if in_position:
                days_held += 1
                forced = max_hold > 0 and days_held >= max_hold
                if rank_pct <= exit_pctl or forced:
                    in_position = False
                    days_held = 0
            elif rank_pct >= entry_pctl:
                in_position = True
                days_held = 0
            pr_signals.append(in_position)
        return pr_signals

    if strategy.name == "dual_momentum_band":
        # YT catalog (Davey's recurring momentum entry): long when long-term
        # momentum is DOWN but the fast lookback has turned up while the slow
        # one is still down — a rebound-within-decline entry. Original exits
        # were profit targets/stops (risk_model levers here); signal-side exit
        # fires when the long-term downtrend condition dies.
        long_lb = int(strategy.parameters["long_lookback"])
        fast_lb = int(strategy.parameters["fast_lookback"])
        slow_lb = int(strategy.parameters["slow_lookback"])
        max_hold = int(strategy.risk_model.get("max_hold_days", 0))
        dm_signals: list[bool] = []
        in_position = False
        days_held = 0
        for index in range(len(closes)):
            if index < long_lb:
                dm_signals.append(False)
                continue
            long_down = closes[index] < closes[index - long_lb]
            fast_up = closes[index] > closes[index - fast_lb]
            slow_down = closes[index] < closes[index - slow_lb]
            if in_position:
                days_held += 1
                forced = max_hold > 0 and days_held >= max_hold
                if not long_down or forced:
                    in_position = False
                    days_held = 0
            elif long_down and fast_up and slow_down:
                in_position = True
                days_held = 0
            dm_signals.append(in_position)
        return dm_signals

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
    vol_target_pct: float = 0.0,
    profit_target_atr: float = 0.0,
    highs: list[float | None] | None = None,
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
    - Profit target: when profit_target_atr > 0, a position exits once the bar
      HIGH touches entry + mult × ATR(14 at entry) — a limit order filled at the
      target, or at the open when the bar gapped above it. When a stop and a
      target could both fire on the same bar, the stop wins (conservative: we
      never assume the favourable intrabar path).
    """
    cost = cost_bps / 10_000.0
    # Shift signals forward one bar to enforce T+1 execution.
    effective = ([False] + signals[:-1]) if signals else []
    stop_fills: dict[int, float] = {}
    if stop_loss_pct > 0 or profit_target_atr > 0:
        effective, stop_fills = _apply_price_exits(
            effective,
            closes,
            stop_loss_pct / 100.0,
            profit_target_atr,
            lows,
            highs,
            opens,
        )

    # Volatility targeting: scale exposure so realised risk approaches a fixed
    # target. The sizing fraction for bar t uses ONLY market returns known by
    # the close of t-1 (trailing window), so there is no look-ahead. Long-only:
    # capped at 1.0 — we de-risk in storms, never lever up in calm.
    size_fractions: list[float] | None = None
    if vol_target_pct > 0:
        daily_target = (vol_target_pct / 100.0) / sqrt(252)
        market_returns = [0.0] + [
            closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
        ]
        size_fractions = [1.0] * len(closes)
        for index in range(1, len(closes)):
            window = market_returns[max(1, index - 20):index]
            if len(window) >= 10:
                realized = pstdev(window)
                if realized > 0:
                    size_fractions[index] = min(1.0, daily_target / realized)

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
            # Trade list stays unsized (per-unit signal quality); the equity
            # curve below reflects the sized performance.
            trades.append((1.0 + gross) * (1.0 - cost) ** 2 - 1.0)  # round-trip net
            bar_return -= cost  # pay exit cost on the bar we exit
            entry_price = None

        if size_fractions is not None:
            bar_return *= size_fractions[index]

        equity *= 1.0 + bar_return
        daily_returns.append(bar_return)
        equity_curve.append(equity)

    if effective and effective[-1] and entry_price is not None:
        gross = closes[-1] / entry_price - 1.0
        trades.append((1.0 + gross) * (1.0 - cost) ** 2 - 1.0)

    return equity_curve, daily_returns, trades


def _entry_atr(
    closes: list[float],
    highs: list[float | None] | None,
    lows: list[float | None] | None,
    index: int,
    period: int = 14,
) -> float:
    """ATR over the `period` bars ending at `index` — true range when OHLC is
    available, close-to-close change otherwise. Used to size profit targets."""
    start = max(1, index - period + 1)
    if start > index:
        return 0.0
    ranges: list[float] = []
    for i in range(start, index + 1):
        high = highs[i] if (highs is not None and highs[i] is not None) else None
        low = lows[i] if (lows is not None and lows[i] is not None) else None
        prev_close = closes[i - 1]
        if high is not None and low is not None:
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        else:
            ranges.append(abs(closes[i] - prev_close))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _apply_price_exits(
    effective: list[bool],
    closes: list[float],
    stop: float,
    target_mult: float,
    lows: list[float | None] | None,
    highs: list[float | None] | None,
    opens: list[float | None] | None = None,
) -> tuple[list[bool], dict[int, float]]:
    """
    Force-exit positions on a stop-loss and/or an ATR-multiple profit target.

    Returns the adjusted signal series plus {bar_index: fill_price} for each
    forced exit.

    Stop (stop > 0): exit once price falls `stop` (fraction) below entry. Fill
    at the stop price, at the open when the bar gapped below it, or at the
    close on close-only data.

    Target (target_mult > 0): exit once the bar HIGH touches
    entry + target_mult × ATR(14 ending at the entry bar) — a resting limit
    order. Fill at the target, at the open when the bar gapped above it, or at
    the close on close-only data (where the close itself must reach the target).

    Same-bar priority: the stop is checked FIRST — when both could have fired
    intrabar we never assume the favourable path. After any forced exit the
    position stays flat until the underlying signal resets (goes flat, then
    fires again), so a forced-out trade is not re-entered on the very next bar.
    """
    result = list(effective)
    fills: dict[int, float] = {}
    entry_price: float | None = None
    target_price: float | None = None
    suppressed = False
    for index in range(len(result)):
        if not result[index]:
            suppressed = False  # strategy flat → clear suppression, no open trade
            entry_price = None
            target_price = None
            continue
        if suppressed:
            result[index] = False  # forced out earlier; remain flat
            continue
        if entry_price is None:
            entry_price = closes[index]  # entry bar — no exit checks yet
            target_price = None
            if target_mult > 0:
                atr = _entry_atr(closes, highs, lows, index)
                if atr > 0:
                    target_price = entry_price + target_mult * atr
            continue

        bar_open = opens[index] if (opens is not None and opens[index] is not None) else None

        # 1. Stop-loss first (conservative same-bar priority).
        if stop > 0:
            stop_price = entry_price * (1.0 - stop)
            low_known = lows is not None and lows[index] is not None
            low = lows[index] if low_known else closes[index]
            if low <= stop_price:
                result[index] = False
                if not low_known:
                    fill = closes[index]  # close-only data: close is the only price
                elif bar_open is not None and bar_open < stop_price:
                    fill = bar_open  # gapped through the stop → filled at the open
                else:
                    fill = stop_price
                fills[index] = fill
                suppressed = True
                entry_price = None
                target_price = None
                continue

        # 2. Profit target.
        if target_price is not None:
            high_known = highs is not None and highs[index] is not None
            high = highs[index] if high_known else closes[index]
            if high >= target_price:
                result[index] = False
                if not high_known:
                    fill = closes[index]  # close-only: the close reached the target
                elif bar_open is not None and bar_open > target_price:
                    fill = bar_open  # gapped above the target → filled at the open
                else:
                    fill = target_price
                fills[index] = fill
                suppressed = True
                entry_price = None
                target_price = None
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

