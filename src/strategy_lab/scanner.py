"""
Cross-sectional market scanner.

Where the backtester is single-symbol and time-series, a scanner is
cross-sectional: on a given day it ranks a *universe* of symbols against each
other and emits a watchlist. We rank symbols by a composite of the indicators
that the IC harness found predictive, each contribution signed by that
indicator's measured edge direction.

Flow:
  evaluate_all_indicators -> pick indicators with |IC| above a threshold
  -> cross_sectional_scan ranks the universe on today's bar -> watchlist.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from strategy_lab.backtest import PriceBar
from strategy_lab.indicators import INDICATORS
from strategy_lab.indicator_eval import PRIMARY_HORIZON, evaluate_all_indicators

DEFAULT_UNIVERSE_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "scanner_universe.yaml"
)


def load_universe(path: Path | str = DEFAULT_UNIVERSE_PATH) -> list[str]:
    import yaml

    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return list(data.get("symbols", []))


def _latest_value(series: list[float | None]) -> float | None:
    for value in reversed(series):
        if value is not None:
            return value
    return None


def _percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Map each symbol's value to its 0..1 rank across the cross-section."""
    ordered = sorted(values.items(), key=lambda item: item[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 0.5}
    return {symbol: position / (n - 1) for position, (symbol, _) in enumerate(ordered)}


def cross_sectional_scan(
    bars_by_symbol: dict[str, list[PriceBar]],
    indicator_directions: dict[str, float],
    top_n: int = 20,
) -> list[dict]:
    """Rank the universe by a direction-signed composite of the given indicators."""
    composite: dict[str, float] = defaultdict(float)
    contributions: dict[str, dict[str, float]] = defaultdict(dict)
    counts: dict[str, int] = defaultdict(int)

    for name, direction in indicator_directions.items():
        fn = INDICATORS[name]
        latest = {}
        for symbol, bars in bars_by_symbol.items():
            value = _latest_value(fn(bars))
            if value is not None:
                latest[symbol] = value
        for symbol, percentile in _percentile_ranks(latest).items():
            # Centre to [-1, 1] and flip by the indicator's edge direction so a
            # higher composite always means "more attractive".
            signed = (percentile - 0.5) * 2.0 * direction
            composite[symbol] += signed
            contributions[symbol][name] = round(signed, 3)
            counts[symbol] += 1

    ranked = [
        {
            "symbol": symbol,
            "composite": round(total / counts[symbol], 4),
            "signals": contributions[symbol],
        }
        for symbol, total in composite.items()
        if counts[symbol] > 0
    ]
    ranked.sort(key=lambda row: row["composite"], reverse=True)
    return ranked[:top_n]


def scan_universe(
    bars_by_symbol: dict[str, list[PriceBar]],
    *,
    min_abs_ic: float = 0.03,
    top_n: int = 20,
    horizon: int = PRIMARY_HORIZON,
) -> dict:
    """Evaluate indicators, keep those with real edge, then rank the universe."""
    ic_results = evaluate_all_indicators(bars_by_symbol, primary_horizon=horizon)
    directions = {
        r["indicator"]: (1.0 if r["primary_ic"] >= 0 else -1.0)
        for r in ic_results
        if r.get("status") == "evaluated" and r["abs_ic"] >= min_abs_ic
    }
    if not directions:
        return {
            "watchlist": [],
            "indicators_used": {},
            "note": f"No indicator cleared the |IC| >= {min_abs_ic} threshold.",
            "ic_table": ic_results,
        }
    watchlist = cross_sectional_scan(bars_by_symbol, directions, top_n=top_n)
    return {
        "watchlist": watchlist,
        "indicators_used": directions,
        "ic_table": ic_results,
    }
