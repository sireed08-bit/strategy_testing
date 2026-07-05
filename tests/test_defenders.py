from datetime import date, timedelta

from strategy_lab.backtest import PriceBar
from strategy_lab.defenders import evaluate_defender
from strategy_lab.models import StrategySpec


def _crash_recovery_bars(symbol: str = "SPY") -> list[PriceBar]:
    """Three synthetic 'years': up, crash (-40%), recovery — enough down-year
    structure for the defender criteria to be exercised."""
    closes = []
    price = 100.0
    for day in range(252):  # year 1: steady up
        price *= 1.0006
        closes.append(price)
    for day in range(252):  # year 2: crash
        price *= 0.998
        closes.append(price)
    for day in range(252):  # year 3: partial recovery then chop, still down
        price *= 0.9995
        closes.append(price)
    start = date(2020, 1, 1)
    out = []
    d = start
    i = 0
    while i < len(closes):
        if d.weekday() < 5:
            out.append(PriceBar(date=d.isoformat(), symbol=symbol, close=closes[i]))
            i += 1
        d += timedelta(days=1)
    return out


def _flat_sitter_spec() -> StrategySpec:
    """rsi_pullback with an impossible entry: never trades → flat through the
    crash. Perfect 'defender geometry' (0 drawdown, 0 loss in down years) even
    though it earns nothing — ideal for testing the criteria mechanics."""
    # A momentum lookback longer than the whole series can never fire: the
    # sleeve is provably flat for the entire test window. (Cheaper and more
    # robust than trying to construct "impossible" RSI conditions — RSI pins to
    # 0 on monotonic declines and 100 on monotonic rallies.)
    return StrategySpec(
        family="calendar", name="day_of_week_momentum", hypothesis="", rules={},
        parameters={"weekday": 2, "momentum_lookback": 100000},
        risk_model={"max_hold_days": 5},
    )


def _record(oos_score=60.0, oos_status="evaluated", dd=0.0) -> dict:
    return {
        "metrics": {"max_drawdown_pct": dd},
        "validation": {"status": oos_status, "oos_score": oos_score},
    }


def test_defender_qualifies_on_crash_avoidance() -> None:
    bars = _crash_recovery_bars()
    result = evaluate_defender(_flat_sitter_spec(), bars, _record())
    assert result["checks"]["oos_ok"] is True
    assert result["checks"]["drawdown_ok"] is True  # 0 dd vs a ~40%+ benchmark dd
    assert result["checks"]["down_years_ok"] is True  # flat beats down years
    assert result["qualified"] is True
    # Blend analysis present and coherent: blending a flat sleeve halves drawdown.
    assert result["blend_50_50_stats"]["max_drawdown_pct"] < result["benchmark_stats"]["max_drawdown_pct"]


def test_defender_rejected_without_oos() -> None:
    bars = _crash_recovery_bars()
    result = evaluate_defender(
        _flat_sitter_spec(), bars, _record(oos_status="inconclusive_few_oos_trades")
    )
    assert result["qualified"] is False
    assert result["checks"]["oos_ok"] is False


def test_defender_rejected_when_drawdown_too_deep() -> None:
    bars = _crash_recovery_bars()
    # Claim a strategy drawdown nearly equal to the benchmark's: fails the bar.
    result = evaluate_defender(_flat_sitter_spec(), bars, _record(dd=39.0))
    assert result["checks"]["drawdown_ok"] is False
    assert result["qualified"] is False
