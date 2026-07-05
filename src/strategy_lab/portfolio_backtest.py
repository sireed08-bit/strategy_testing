"""
Multi-symbol "switch" engine (v1): hold exactly one symbol from a set — or cash.

The single-symbol engine cannot express the strategy classes with the best
cross-regime evidence: rotation (hold the strongest of N) and risk-off (hold
bonds when stocks break down). This v1 keeps the portfolio problem deliberately
small — one holding at a time, long-only, T+1 switching, per-leg costs — which
is enough to express:

  - regime_switch_pair: risk asset above its trend SMA → hold it; else the safe
    asset. The strategy `spy_tlt_regime_switch` always promised this and never
    actually held TLT.
  - relative_momentum_rotation: at each rebalance, hold the symbol with the
    strongest trailing return — cash if none is positive.
  - bond_low_risk_off: the mined-catalog cross-asset signal — when the bond
    close sits at an N-day low (rates spiking), step out of equities to cash.

Metrics reuse assemble_metrics; the benchmark is buy-and-hold of the FIRST
symbol in the spec (the risk/equity leg), so excess return answers "did the
switching beat just holding the risk asset?"
"""
from __future__ import annotations

from strategy_lab.backtest import PriceBar, assemble_metrics, sma
from strategy_lab.models import StrategySpec


def align_bars(bars_by_symbol: dict[str, list[PriceBar]]) -> tuple[list[str], dict[str, list[float]]]:
    """Intersect trading dates across symbols; return sorted dates + aligned closes."""
    if not bars_by_symbol:
        return [], {}
    date_sets = [
        {bar.date for bar in bars} for bars in bars_by_symbol.values()
    ]
    common = sorted(set.intersection(*date_sets))
    closes_by_symbol: dict[str, list[float]] = {}
    for symbol, bars in bars_by_symbol.items():
        lookup = {bar.date: bar.close for bar in bars}
        closes_by_symbol[symbol] = [lookup[d] for d in common]
    return common, closes_by_symbol


def build_allocation(
    strategy: StrategySpec,
    dates: list[str],
    closes_by_symbol: dict[str, list[float]],
) -> list[str | None]:
    """Per-bar target holding: a symbol name, or None for cash."""
    n = len(dates)

    if strategy.name == "regime_switch_pair":
        risk = strategy.parameters["risk_symbol"]
        safe = strategy.parameters["safe_symbol"]
        trend = int(strategy.parameters["trend_sma"])
        risk_closes = closes_by_symbol[risk]
        allocation: list[str | None] = []
        for i in range(n):
            if i < trend:
                allocation.append(None)  # warmup: cash
            elif risk_closes[i] > sma(risk_closes, i, trend):
                allocation.append(risk)
            else:
                allocation.append(safe)
        return allocation

    if strategy.name == "relative_momentum_rotation":
        symbols = list(strategy.parameters["symbols"])
        lookback = int(strategy.parameters["lookback"])
        rebalance = int(strategy.parameters["rebalance_days"])
        allocation = []
        current: str | None = None
        for i in range(n):
            if i < lookback:
                allocation.append(None)
                continue
            if (i - lookback) % rebalance == 0:
                best_symbol, best_ret = None, 0.0
                for symbol in symbols:
                    closes = closes_by_symbol[symbol]
                    ret = closes[i] / closes[i - lookback] - 1.0
                    if ret > best_ret:
                        best_symbol, best_ret = symbol, ret
                current = best_symbol  # None when no trailing return is positive
            allocation.append(current)
        return allocation

    if strategy.name == "bond_low_risk_off":
        equity = strategy.parameters["equity_symbol"]
        bond = strategy.parameters["bond_symbol"]
        low_lb = int(strategy.parameters["low_lookback"])
        bond_closes = closes_by_symbol[bond]
        allocation = []
        for i in range(n):
            if i < low_lb:
                allocation.append(None)
                continue
            # Strictly below the prior window's minimum = a NEW low. A flat
            # series must not read as perpetually "at its low".
            prior_min = min(bond_closes[i - low_lb : i])
            if bond_closes[i] < prior_min:
                allocation.append(None)  # bond making new lows → risk-off, cash
            else:
                allocation.append(equity)
        return allocation

    raise ValueError(f"No portfolio implementation for strategy: {strategy.name}")


def simulate_switch(
    dates: list[str],
    closes_by_symbol: dict[str, list[float]],
    allocation: list[str | None],
    cost_bps: float,
) -> tuple[list[float], list[float], list[float]]:
    """
    Equity simulation for one-holding-at-a-time switching.

    T+1: the allocation decided on bar t is held over bar t+1's return. Every
    leg change pays cost_bps per leg — a switch from A to B costs two legs
    (sell A, buy B); entering from or exiting to cash costs one.
    """
    cost = cost_bps / 10_000.0
    effective: list[str | None] = [None] + allocation[:-1]

    equity = 1.0
    equity_curve = [equity]
    daily_returns: list[float] = []
    trades: list[float] = []
    holding_return = 1.0
    for i in range(1, len(dates)):
        held = effective[i - 1]
        nxt = effective[i]

        bar_return = 0.0
        if held is not None:
            closes = closes_by_symbol[held]
            bar_return = closes[i] / closes[i - 1] - 1.0
            holding_return *= 1.0 + bar_return

        if held != nxt:
            legs = (1 if held is not None else 0) + (1 if nxt is not None else 0)
            bar_return -= cost * legs
            if held is not None:
                trades.append(holding_return * (1.0 - cost) ** 2 - 1.0)
            holding_return = 1.0

        equity *= 1.0 + bar_return
        daily_returns.append(bar_return)
        equity_curve.append(equity)

    if effective and effective[-1] is not None:
        trades.append(holding_return * (1.0 - cost) ** 2 - 1.0)
    return equity_curve, daily_returns, trades


def run_portfolio_backtest(
    strategy: StrategySpec,
    bars_by_symbol: dict[str, list[PriceBar]],
    cost_bps: float = 5.0,
    start_index: int = 0,
) -> dict[str, float]:
    """
    Metrics for a switch strategy over aligned bars, optionally only the window
    from start_index (allocation built on the FULL series — warm, like
    run_backtest_window). Benchmark = buy-and-hold of the first/risk symbol.
    """
    dates, closes_by_symbol = align_bars(bars_by_symbol)
    if len(dates) < 2 or start_index >= len(dates) - 1:
        raise ValueError("Not enough aligned bars for a portfolio backtest.")
    allocation = build_allocation(strategy, dates, closes_by_symbol)

    window_alloc = allocation[start_index:]
    window_closes = {s: c[start_index:] for s, c in closes_by_symbol.items()}
    window_dates = dates[start_index:]
    equity_curve, daily_returns, trades = simulate_switch(
        window_dates, window_closes, window_alloc, cost_bps
    )
    benchmark_symbol = _benchmark_symbol(strategy)
    signals = [a is not None for a in window_alloc]
    return assemble_metrics(
        equity_curve, daily_returns, trades, signals, window_closes[benchmark_symbol]
    )


def evaluate_portfolio(
    strategy: StrategySpec,
    bars_by_symbol: dict[str, list[PriceBar]],
) -> tuple[dict, dict, dict]:
    """
    Full evaluation under the lab's standard discipline: the newest
    FINAL_EXAM_FRACTION of aligned history is excluded from everything, the
    remainder is scored in full and out-of-sample (train fraction vs warm
    held-out window). Returns (metrics, validation, dataset_fields).
    """
    from strategy_lab.batch_runner import (
        FINAL_EXAM_FRACTION,
        MIN_OOS_TRADES,
        OOS_FAIL_SCORE,
        TRAIN_FRACTION,
    )
    from strategy_lab.scoring import score_metrics

    dates, closes_by_symbol = align_bars(bars_by_symbol)
    exam_start = int(round(len(dates) * (1.0 - FINAL_EXAM_FRACTION)))
    opt_dates = dates[:exam_start] if exam_start >= 120 else dates
    trimmed = {
        symbol: [b for b in bars if b.date <= opt_dates[-1]]
        for symbol, bars in bars_by_symbol.items()
    }

    metrics = run_portfolio_backtest(strategy, trimmed)

    split = int(len(opt_dates) * TRAIN_FRACTION)
    validation: dict = {"status": "insufficient_data"}
    weak_oos = False
    if split >= 60 and len(opt_dates) - split >= 60:
        train_bars = {
            symbol: [b for b in bars if b.date <= opt_dates[split - 1]]
            for symbol, bars in trimmed.items()
        }
        train_score = score_metrics(run_portfolio_backtest(strategy, train_bars)).score
        oos_metrics = run_portfolio_backtest(strategy, trimmed, start_index=split)
        oos = score_metrics(oos_metrics)
        validation = {
            "status": "evaluated",
            "train_frac": TRAIN_FRACTION,
            "train_score": train_score,
            "oos_score": oos.score,
            "oos_grade": oos.grade,
            "degradation": round(train_score - oos.score, 2),
            "oos_trade_count": oos_metrics["trade_count"],
            "warm_indicators": True,
        }
        if oos_metrics["trade_count"] < MIN_OOS_TRADES:
            validation["status"] = "inconclusive_few_oos_trades"
        elif oos.score < OOS_FAIL_SCORE:
            weak_oos = True

    dataset_fields = {
        "symbols": sorted(bars_by_symbol.keys()),
        "start": opt_dates[0],
        "end": opt_dates[-1],
    }
    if weak_oos:
        validation["failed_oos"] = True
    return metrics, validation, dataset_fields


def _benchmark_symbol(strategy: StrategySpec) -> str:
    for key in ("risk_symbol", "equity_symbol"):
        if key in strategy.parameters:
            return strategy.parameters[key]
    return list(strategy.parameters["symbols"])[0]


def portfolio_symbols(strategy: StrategySpec) -> list[str]:
    """Every symbol a spec needs data for."""
    p = strategy.parameters
    if "symbols" in p:
        return list(p["symbols"])
    return [v for k, v in p.items() if k.endswith("_symbol")]
