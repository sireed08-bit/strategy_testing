from strategy_lab.backtest import PriceBar
from strategy_lab.models import StrategySpec
from strategy_lab.portfolio_backtest import (
    align_bars,
    build_allocation,
    portfolio_symbols,
    run_portfolio_backtest,
    simulate_switch,
)


def _bars(symbol: str, closes: list[float], skip_dates: set[str] | None = None) -> list[PriceBar]:
    out = []
    day = 0
    i = 0
    while i < len(closes):
        day += 1
        date = f"2025-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}"
        if skip_dates and date in skip_dates:
            continue
        out.append(PriceBar(date=date, symbol=symbol, close=closes[i]))
        i += 1
    return out


def test_align_bars_intersects_dates() -> None:
    a = _bars("AAA", [1.0, 2.0, 3.0, 4.0])
    b = _bars("BBB", [10.0, 20.0, 30.0])  # one fewer bar
    dates, closes = align_bars({"AAA": a, "BBB": b})
    assert len(dates) == 3
    assert closes["AAA"] == [1.0, 2.0, 3.0]
    assert closes["BBB"] == [10.0, 20.0, 30.0]


def test_regime_switch_holds_risk_above_sma_and_safe_below() -> None:
    # Risk asset trends up for 30 bars then crashes; safe asset is flat.
    risk = [100.0 + i for i in range(30)] + [90.0, 80.0, 70.0, 60.0]
    safe = [50.0] * len(risk)
    spec = StrategySpec(
        family="risk_on_risk_off", name="regime_switch_pair", hypothesis="", rules={},
        parameters={"risk_symbol": "RSK", "safe_symbol": "SAF", "trend_sma": 10},
        risk_model={},
    )
    dates, closes = align_bars({"RSK": _bars("RSK", risk), "SAF": _bars("SAF", safe)})
    allocation = build_allocation(spec, dates, closes)
    assert allocation[20] == "RSK"   # uptrend: hold risk
    assert allocation[-1] == "SAF"   # after the crash: below SMA → safe


def test_rotation_picks_strongest_and_cash_when_all_negative() -> None:
    up = [100.0 * (1.01 ** i) for i in range(40)]
    flat = [100.0] * 40
    down = [100.0 * (0.99 ** i) for i in range(40)]
    spec = StrategySpec(
        family="momentum", name="relative_momentum_rotation", hypothesis="", rules={},
        parameters={"symbols": ["UP", "FLAT"], "lookback": 20, "rebalance_days": 5},
        risk_model={},
    )
    dates, closes = align_bars({"UP": _bars("UP", up), "FLAT": _bars("FLAT", flat)})
    allocation = build_allocation(spec, dates, closes)
    assert allocation[25] == "UP"

    spec_down = StrategySpec(
        family="momentum", name="relative_momentum_rotation", hypothesis="", rules={},
        parameters={"symbols": ["DOWN", "FLAT"], "lookback": 20, "rebalance_days": 5},
        risk_model={},
    )
    dates2, closes2 = align_bars({"DOWN": _bars("DOWN", down), "FLAT": _bars("FLAT", flat)})
    allocation2 = build_allocation(spec_down, dates2, closes2)
    assert allocation2[25] is None  # nothing positive → cash


def test_bond_low_steps_to_cash() -> None:
    equity = [100.0] * 30
    bond = [50.0] * 20 + [49.0, 48.0, 47.0] + [47.5] * 7  # new lows at bars 20-22
    spec = StrategySpec(
        family="risk_on_risk_off", name="bond_low_risk_off", hypothesis="", rules={},
        parameters={"equity_symbol": "EQ", "bond_symbol": "BD", "low_lookback": 10},
        risk_model={},
    )
    dates, closes = align_bars({"EQ": _bars("EQ", equity), "BD": _bars("BD", bond)})
    allocation = build_allocation(spec, dates, closes)
    assert allocation[15] == "EQ"   # bond stable → hold equity
    assert allocation[21] is None   # bond at N-day low → cash


def test_simulate_switch_charges_per_leg_costs() -> None:
    dates = [f"2025-01-{d:02d}" for d in range(1, 6)]
    closes = {"A": [100.0, 100.0, 100.0, 100.0, 100.0], "B": [50.0, 50.0, 50.0, 50.0, 50.0]}
    # Switch A -> B mid-way on flat prices: only costs should reduce equity.
    allocation = ["A", "A", "B", "B", "B"]
    curve_free, _, _ = simulate_switch(dates, closes, allocation, cost_bps=0.0)
    curve_cost, _, _ = simulate_switch(dates, closes, allocation, cost_bps=10.0)
    assert curve_free[-1] == 1.0
    assert curve_cost[-1] < curve_free[-1]


def test_run_portfolio_backtest_produces_standard_metrics() -> None:
    risk = [100.0 + i * 0.5 for i in range(300)]
    safe = [50.0 + i * 0.05 for i in range(300)]
    spec = StrategySpec(
        family="risk_on_risk_off", name="regime_switch_pair", hypothesis="", rules={},
        parameters={"risk_symbol": "RSK", "safe_symbol": "SAF", "trend_sma": 50},
        risk_model={},
    )
    metrics = run_portfolio_backtest(spec, {"RSK": _bars("RSK", risk), "SAF": _bars("SAF", safe)})
    assert "excess_return_pct" in metrics and "benchmark_return_pct" in metrics
    assert portfolio_symbols(spec) == ["RSK", "SAF"]
