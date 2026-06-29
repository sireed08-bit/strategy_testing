from datetime import date, timedelta

from strategy_lab.auto_research import _perturb, propose_refinements
from strategy_lab.backtest import PriceBar, build_signals_from_bars, run_backtest
from strategy_lab.models import DatasetSpec, StrategySpec


def _ohlcv(n: int = 320, symbol: str = "SPY") -> list[PriceBar]:
    import math

    start = date(2021, 1, 1)
    bars = []
    price = 100.0
    for d in range(n):
        price = max(1.0, price * (1.0 + 0.0003) + 6.0 * math.sin(d * 0.18))
        close = round(price, 4)
        bars.append(
            PriceBar(
                date=(start + timedelta(days=d)).isoformat(),
                symbol=symbol,
                close=close,
                open=round(close * (1.0 + 0.002 * math.sin(d)), 4),
                high=close * 1.01,
                low=close * 0.985,
                volume=1_000_000.0,
                vwap=close,
            )
        )
    return bars


def test_sma_reversion_produces_valid_backtest() -> None:
    spec = StrategySpec(
        family="mean_reversion",
        name="sma_reversion",
        hypothesis="",
        rules={},
        parameters={"sma_window": 50, "entry_pct": 5, "exit_pct": 1},
        risk_model={"max_hold_days": 10, "stop_loss_pct": 8},
    )
    metrics = run_backtest(spec, _ohlcv())
    assert metrics["trade_count"] >= 0
    assert "max_drawdown_pct" in metrics


def test_gap_momentum_requires_open_and_runs() -> None:
    spec = StrategySpec(
        family="momentum",
        name="gap_momentum",
        hypothesis="",
        rules={},
        parameters={"gap_pct": 1.0},
        risk_model={"max_hold_days": 3, "stop_loss_pct": 5},
    )
    # With OHLCV present it produces a real signal series.
    signals = build_signals_from_bars(spec, _ohlcv())
    assert len(signals) == 320
    # Without open prices it safely yields no signals rather than crashing.
    close_only = [PriceBar(date=f"2021-02-{d:02d}", symbol="SPY", close=100.0 + d) for d in range(1, 20)]
    assert build_signals_from_bars(spec, close_only) == [False] * len(close_only)


def test_perturb_generates_neighbors() -> None:
    assert _perturb(14) == [12, 16]  # step = round(14*0.15) = 2
    assert all(v > 0 for v in _perturb(10))
    assert _perturb(True) == []
    assert _perturb("text") == []


def test_propose_refinements_creates_fresh_neighbors() -> None:
    dataset = DatasetSpec(name="t", symbols=["QQQ"], timeframe="1D", start="2021-01-01", end="2021-12-31")
    records = [
        {
            "strategy": {
                "name": "rsi_pullback",
                "family": "mean_reversion",
                "hypothesis": "",
                "rules": {},
                "parameters": {"entry_rsi": 40, "exit_rsi": 65, "rsi_period": 14, "sma_filter": 200},
                "risk_model": {"max_hold_days": 15},
            },
            "dataset": {"symbols": ["QQQ"]},
            "score": 72.0,
            "grade": "promising",
        }
    ]
    proposals = propose_refinements(records, dataset, existing_fingerprints=set(), top_k=3, max_new=20)
    assert proposals
    assert all(p.name == "rsi_pullback" for p in proposals)
    # Each neighbour differs from the seed in exactly one tunable value.
    seed = {"entry_rsi": 40, "exit_rsi": 65, "rsi_period": 14, "sma_filter": 200, "max_hold_days": 15}
    for p in proposals:
        combined = {**p.parameters, **p.risk_model}
        diffs = sum(1 for k in seed if combined.get(k) != seed[k])
        assert diffs == 1
