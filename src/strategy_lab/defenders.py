"""
Defender designation — harvesting what the lab verifiably finds.

The 21-year verdict was unambiguous: long-only timing on liquid ETFs does not
out-compound buy-and-hold. But the same run showed the system's best finds are
DEFENSIVE: −1.4% through 2008 against the market's −32%, single-digit drawdowns
across two decades. The champion bar (positive excess) correctly refuses those;
this module gives them their own, equally strict bar:

  A defender must
    - be out-of-sample evaluated with a passing score,
    - draw down less than half of what its benchmark drew down,
    - deliver positive excess in most of the benchmark's DOWN years
      (with at least two such years observed).

And because a pure defender still lags in bull markets, the blend analysis
answers the question an allocator actually asks: does 50% defender + 50%
buy-and-hold beat 100% buy-and-hold risk-adjusted?
"""
from __future__ import annotations

from statistics import mean, pstdev
from math import sqrt

from strategy_lab.backtest import (
    COST_BPS,
    PriceBar,
    build_signals_from_bars,
    max_drawdown_pct,
    simulate_long_only,
    yearly_breakdown,
)
from strategy_lab.models import StrategySpec

MIN_DOWN_YEARS = 2
DOWN_YEAR_WIN_FRACTION = 0.66
MAX_DD_FRACTION_OF_BENCHMARK = 0.5
MIN_OOS_SCORE = 45.0


def _strategy_daily_returns(strategy: StrategySpec, bars: list[PriceBar]) -> list[float]:
    ordered = sorted(bars, key=lambda bar: bar.date)
    closes = [bar.close for bar in ordered]
    signals = build_signals_from_bars(strategy, ordered)
    _, daily_returns, _ = simulate_long_only(
        closes,
        signals,
        COST_BPS,
        stop_loss_pct=float(strategy.risk_model.get("stop_loss_pct", 0)),
        lows=[bar.low for bar in ordered],
        opens=[bar.open for bar in ordered],
        highs=[bar.high for bar in ordered],
        vol_target_pct=float(strategy.risk_model.get("vol_target_pct", 0)),
        profit_target_atr=float(strategy.risk_model.get("profit_target_atr", 0)),
    )
    return daily_returns


def _curve(returns: list[float]) -> list[float]:
    equity = 1.0
    curve = [equity]
    for r in returns:
        equity *= 1.0 + r
        curve.append(equity)
    return curve


def _stats(returns: list[float]) -> dict:
    curve = _curve(returns)
    years = max(len(returns) / 252.0, 1e-9)
    annualized = (curve[-1] ** (1.0 / years) - 1.0) * 100.0
    sharpe = 0.0
    if len(returns) > 1 and pstdev(returns) > 0:
        sharpe = (mean(returns) / pstdev(returns)) * sqrt(252)
    return {
        "annualized_pct": round(annualized, 2),
        "max_drawdown_pct": max_drawdown_pct(curve),
        "sharpe": round(sharpe, 2),
    }


def evaluate_defender(
    strategy: StrategySpec,
    bars: list[PriceBar],
    record: dict,
) -> dict:
    """
    Assess one record against the defender bar and compute the 50/50 blend.
    `bars` must be the same pinned, exam-trimmed window the record was scored on.
    """
    validation = record.get("validation") or {}
    oos_ok = (
        validation.get("status") == "evaluated"
        and (validation.get("oos_score") or 0) >= MIN_OOS_SCORE
    )

    ordered = sorted(bars, key=lambda bar: bar.date)
    closes = [bar.close for bar in ordered]
    benchmark_returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    benchmark_dd = max_drawdown_pct(_curve(benchmark_returns))
    strategy_dd = record["metrics"]["max_drawdown_pct"]
    dd_ok = benchmark_dd > 0 and strategy_dd <= MAX_DD_FRACTION_OF_BENCHMARK * benchmark_dd

    years = yearly_breakdown(strategy, ordered)
    down_years = [y for y in years if y["benchmark_pct"] < 0]
    wins_in_down = [y for y in down_years if y["excess_pct"] > 0]
    down_ok = (
        len(down_years) >= MIN_DOWN_YEARS
        and len(wins_in_down) / len(down_years) >= DOWN_YEAR_WIN_FRACTION
    )

    qualified = oos_ok and dd_ok and down_ok

    result = {
        "qualified": qualified,
        "checks": {
            "oos_ok": oos_ok,
            "drawdown_ok": dd_ok,
            "down_years_ok": down_ok,
        },
        "strategy_dd_pct": strategy_dd,
        "benchmark_dd_pct": benchmark_dd,
        "down_years": f"{len(wins_in_down)}/{len(down_years)} beaten",
    }

    if qualified:
        strat_returns = _strategy_daily_returns(strategy, ordered)
        n = min(len(strat_returns), len(benchmark_returns))
        blend_returns = [
            0.5 * strat_returns[i] + 0.5 * benchmark_returns[i] for i in range(n)
        ]
        result["benchmark_stats"] = _stats(benchmark_returns[:n])
        result["blend_50_50_stats"] = _stats(blend_returns)
        result["blend_improves_sharpe"] = (
            result["blend_50_50_stats"]["sharpe"] > result["benchmark_stats"]["sharpe"]
        )
    return result
