"""
Indicator evaluation harness — ranks indicators by predictive edge.

For each indicator we measure its Information Coefficient (IC): the Spearman
rank correlation between the indicator's value at time t and the forward return
over a horizon h. A consistently non-zero IC means the indicator carries real
predictive signal; IC near zero means it is noise for that horizon.

We report, per indicator:
  - IC at each horizon (1/5/20 days) and how the edge decays across horizons
  - directional hit-rate (how often the indicator's sign predicts the move)
  - cross-symbol IC spread (does the edge hold across instruments, or is it one
    lucky symbol?)

This is the engine that answers "which indicators are best to scan on."
"""
from __future__ import annotations

from statistics import mean, pstdev

from strategy_lab.backtest import PriceBar
from strategy_lab.indicators import INDICATORS, Series

HORIZONS = (1, 5, 20)
PRIMARY_HORIZON = 5
_MIN_OBSERVATIONS = 50


def forward_return_series(bars: list[PriceBar], horizon: int) -> Series:
    closes = [bar.close for bar in bars]
    out: Series = [None] * len(closes)
    for index in range(len(closes) - horizon):
        base = closes[index]
        if base:
            out[index] = closes[index + horizon] / base - 1.0
    return out


def _ranks(values: list[float]) -> list[float]:
    """Average-rank transform (ties share the mean of their rank positions)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx**0.5 * vy**0.5)


def spearman(xs: list[float], ys: list[float]) -> float:
    return _pearson(_ranks(xs), _ranks(ys))


def _paired(indicator: Series, forward: Series) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for value, fwd in zip(indicator, forward):
        if value is not None and fwd is not None:
            xs.append(value)
            ys.append(fwd)
    return xs, ys


def evaluate_indicator(
    bars_by_symbol: dict[str, list[PriceBar]],
    indicator_fn,
    horizon: int,
) -> dict | None:
    pooled_x: list[float] = []
    pooled_y: list[float] = []
    ic_by_symbol: list[float] = []

    for bars in bars_by_symbol.values():
        indicator = indicator_fn(bars)
        forward = forward_return_series(bars, horizon)
        xs, ys = _paired(indicator, forward)
        if len(xs) >= _MIN_OBSERVATIONS:
            ic_by_symbol.append(spearman(xs, ys))
        pooled_x.extend(xs)
        pooled_y.extend(ys)

    if len(pooled_x) < _MIN_OBSERVATIONS:
        return None

    ic = spearman(pooled_x, pooled_y)
    median_x = sorted(pooled_x)[len(pooled_x) // 2]
    # Directional hit-rate: does being above/below the indicator's median predict
    # the sign of the forward return? Aligned to the IC's own direction.
    sign = 1.0 if ic >= 0 else -1.0
    hits = sum(
        1
        for x, y in zip(pooled_x, pooled_y)
        if (x - median_x) * y * sign > 0
    )
    decisive = sum(1 for x, y in zip(pooled_x, pooled_y) if (x - median_x) != 0 and y != 0)

    return {
        "horizon": horizon,
        "ic": round(ic, 4),
        "abs_ic": round(abs(ic), 4),
        "observations": len(pooled_x),
        "hit_rate": round(hits / decisive, 4) if decisive else None,
        "cross_symbol_ic_spread": round(pstdev(ic_by_symbol), 4) if len(ic_by_symbol) > 1 else None,
        "symbols_evaluated": len(ic_by_symbol),
    }


def evaluate_all_indicators(
    bars_by_symbol: dict[str, list[PriceBar]],
    horizons: tuple[int, ...] = HORIZONS,
    primary_horizon: int = PRIMARY_HORIZON,
) -> list[dict]:
    """Rank every registered indicator by |IC| at the primary horizon."""
    results: list[dict] = []
    for name, fn in INDICATORS.items():
        by_horizon = {}
        for horizon in horizons:
            evaluation = evaluate_indicator(bars_by_symbol, fn, horizon)
            if evaluation is not None:
                by_horizon[horizon] = evaluation
        if not by_horizon:
            results.append({"indicator": name, "status": "insufficient_data"})
            continue
        primary = by_horizon.get(primary_horizon) or next(iter(by_horizon.values()))
        results.append(
            {
                "indicator": name,
                "status": "evaluated",
                "primary_horizon": primary["horizon"],
                "primary_ic": primary["ic"],
                "abs_ic": primary["abs_ic"],
                "hit_rate": primary["hit_rate"],
                "cross_symbol_ic_spread": primary["cross_symbol_ic_spread"],
                "by_horizon": {h: by_horizon[h]["ic"] for h in sorted(by_horizon)},
            }
        )

    results.sort(key=lambda r: r.get("abs_ic", -1.0), reverse=True)
    return results
