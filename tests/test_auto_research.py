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


def _record(
    name: str,
    params: dict,
    score: float,
    *,
    symbol: str = "QQQ",
    grade: str = "promising",
    oos_score: float | None = 70.0,
    oos_trades: float = 12.0,
    oos_status: str = "evaluated",
    risk: dict | None = None,
) -> dict:
    validation = (
        {"status": oos_status, "oos_score": oos_score, "oos_trade_count": oos_trades}
        if oos_score is not None
        else {}
    )
    return {
        "strategy": {
            "name": name,
            "family": "mean_reversion",
            "hypothesis": "",
            "rules": {},
            "parameters": params,
            "risk_model": risk if risk is not None else {"max_hold_days": 15},
        },
        "dataset": {"symbols": [symbol]},
        "score": score,
        "grade": grade,
        "validation": validation,
    }


def test_propose_refinements_creates_fresh_neighbors() -> None:
    dataset = DatasetSpec(name="t", symbols=["QQQ"], timeframe="1D", start="2021-01-01", end="2021-12-31")
    records = [
        _record("rsi_pullback", {"entry_rsi": 40, "exit_rsi": 65, "rsi_period": 14, "sma_filter": 200}, 72.0)
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


def test_propose_refinements_skips_oos_unproven_seeds() -> None:
    """The documented guarantee: only OOS-validated records may seed refinement."""
    dataset = DatasetSpec(name="t", symbols=["QQQ"], timeframe="1D", start="2021-01-01", end="2021-12-31")
    base_params = {"entry_rsi": 40, "exit_rsi": 65, "rsi_period": 14, "sma_filter": 200}
    ineligible = [
        _record("rsi_pullback", base_params, 90.0, oos_score=None),  # never validated
        _record("rsi_pullback", base_params, 89.0, oos_status="inconclusive_few_oos_trades"),
        _record("rsi_pullback", base_params, 88.0, oos_score=30.0),  # failed OOS
        _record("rsi_pullback", base_params, 87.0, oos_trades=2.0),  # too few OOS trades
        _record("rsi_pullback", base_params, 86.0, grade="reject"),
    ]
    assert propose_refinements(ineligible, dataset, existing_fingerprints=set(), top_k=6, max_new=20) == []


def test_seed_ranking_prefers_weakest_evidence_over_raw_score() -> None:
    """A lower-scored seed with strong OOS outranks a high scorer with weak OOS."""
    from strategy_lab.auto_research import select_seeds

    flashy = _record(
        "rsi_pullback", {"entry_rsi": 40, "exit_rsi": 86, "rsi_period": 14, "sma_filter": 200},
        82.0, oos_score=47.0,
    )
    solid = _record(
        "rsi_pullback", {"entry_rsi": 35, "exit_rsi": 60, "rsi_period": 14, "sma_filter": 200},
        71.0, oos_score=72.0,
    )
    seeds = select_seeds([flashy, solid], top_k=2)
    assert seeds[0]["strategy"]["parameters"]["exit_rsi"] == 60  # solid first


def test_seed_pool_caps_one_family_from_flooding() -> None:
    from strategy_lab.auto_research import MAX_SEEDS_PER_FAMILY, select_seeds

    corner = [
        _record("rsi_pullback", {"entry_rsi": 40, "exit_rsi": 60 + i, "rsi_period": 14, "sma_filter": 200}, 78.0 - i)
        for i in range(5)
    ]
    other = [_record("gap_momentum", {"gap_pct": 1.0}, 66.0, risk={"max_hold_days": 3})]
    seeds = select_seeds(corner + other, top_k=4)
    by_name = [s["strategy"]["name"] for s in seeds]
    assert by_name.count("rsi_pullback") <= MAX_SEEDS_PER_FAMILY
    assert "gap_momentum" in by_name  # the minority family still gets explored


def test_propose_explorations_samples_within_grid_ranges() -> None:
    import random

    from strategy_lab.auto_research import propose_explorations

    dataset = DatasetSpec(name="t", symbols=["QQQ"], timeframe="1D", start="2021-01-01", end="2021-12-31")
    space = {
        "strategies": [
            {
                "family": "mean_reversion",
                "name": "rsi_pullback",
                "hypothesis": "",
                "rules": {},
                "parameter_grid": {
                    "rsi_period": [7, 14, 28],
                    "entry_rsi": [20, 45],
                    "exit_rsi": [50, 70],
                    "sma_filter": [50, 300],
                },
                "risk_grid": {"max_hold_days": [5, 15]},
            }
        ]
    }
    rng = random.Random(42)
    seen: set[str] = set()
    proposals = propose_explorations(dataset, seen, count=10, rng=rng, experiment_space=space)
    assert len(proposals) == 10
    for p in proposals:
        assert 7 <= p.parameters["rsi_period"] <= 28
        assert 20 <= p.parameters["entry_rsi"] <= 45
        assert 50 <= p.parameters["exit_rsi"] <= 70
        assert 5 <= p.risk_model["max_hold_days"] <= 15
    # Deterministic: the same seed reproduces the same draw.
    proposals_again = propose_explorations(
        dataset, set(), count=10, rng=random.Random(42), experiment_space=space
    )
    assert [p.parameters for p in proposals] == [p.parameters for p in proposals_again]


def test_cross_symbol_support_counts_sibling_symbols() -> None:
    from strategy_lab.analysis import cross_symbol_support

    params = {"entry_rsi": 40, "exit_rsi": 60, "rsi_period": 14}
    qqq = _record("rsi_pullback", params, 70.0, symbol="QQQ")
    qqq["fingerprint"] = "fp-qqq"
    iwm = _record("rsi_pullback", params, 66.0, symbol="IWM")
    iwm["fingerprint"] = "fp-iwm"
    spy_reject = _record("rsi_pullback", params, 30.0, symbol="SPY", grade="reject")
    spy_reject["fingerprint"] = "fp-spy"
    lonely = _record("rsi_pullback", {**params, "entry_rsi": 20}, 68.0, symbol="QQQ")
    lonely["fingerprint"] = "fp-lonely"

    support = cross_symbol_support([qqq, iwm, spy_reject, lonely])
    assert support["fp-qqq"] == 1  # IWM confirms; reject-grade SPY does not count
    assert support["fp-iwm"] == 1
    assert support["fp-lonely"] == 0  # different params — no siblings


def test_select_seeds_prefers_cross_symbol_confirmed_combos() -> None:
    from strategy_lab.auto_research import select_seeds

    params_confirmed = {"entry_rsi": 35, "exit_rsi": 60, "rsi_period": 7, "sma_filter": 50}
    confirmed = _record("rsi_pullback", params_confirmed, 66.0, oos_score=66.0)
    confirmed["fingerprint"] = "fp-confirmed"
    higher_but_lonely = _record(
        "rsi_pullback",
        {"entry_rsi": 40, "exit_rsi": 70, "rsi_period": 14, "sma_filter": 200},
        74.0,
        oos_score=74.0,
    )
    higher_but_lonely["fingerprint"] = "fp-lonely"

    seeds = select_seeds(
        [confirmed, higher_but_lonely],
        top_k=2,
        symbol_support={"fp-confirmed": 2, "fp-lonely": 0},
    )
    # The cross-symbol-confirmed combo leads despite the lower score.
    assert seeds[0]["fingerprint"] == "fp-confirmed"


def test_portfolio_records_never_seed_single_symbol_refinement() -> None:
    from strategy_lab.auto_research import select_seeds

    portfolio = _record(
        "regime_switch_pair",
        {"risk_symbol": "SPY", "safe_symbol": "TLT", "trend_sma": 200},
        80.0, oos_score=80.0,
    )
    portfolio["fingerprint"] = "fp-port"
    ordinary = _record(
        "rsi_pullback", {"entry_rsi": 40, "exit_rsi": 60, "rsi_period": 14, "sma_filter": 200},
        66.0, oos_score=66.0,
    )
    ordinary["fingerprint"] = "fp-ord"
    seeds = select_seeds([portfolio, ordinary], top_k=2)
    names = [s["strategy"]["name"] for s in seeds]
    assert "regime_switch_pair" not in names  # would error in the single-symbol engine
    assert "rsi_pullback" in names


def test_perturbation_respects_rsi_bounds() -> None:
    """exit_rsi must never be pushed past 99 — an RSI exit above 100 can never fire."""
    dataset = DatasetSpec(name="t", symbols=["QQQ"], timeframe="1D", start="2021-01-01", end="2021-12-31")
    records = [
        _record("rsi_pullback", {"entry_rsi": 40, "exit_rsi": 95, "rsi_period": 14, "sma_filter": 200}, 75.0)
    ]
    proposals = propose_refinements(records, dataset, existing_fingerprints=set(), top_k=1, max_new=50)
    for p in proposals:
        assert p.parameters["exit_rsi"] <= 99
        assert p.parameters["entry_rsi"] <= 99
