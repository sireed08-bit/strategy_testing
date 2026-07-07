from strategy_lab.backtest import PriceBar, run_backtest_window
from strategy_lab.batch_runner import (
    evaluate_and_log_strategies,
    final_exam_start_index,
    out_of_sample_validation,
    run_backtest_batch,
    trim_final_exam,
)
from strategy_lab.experiment_log import ExperimentLog
from strategy_lab.models import StrategySpec
from strategy_lab.run_ledger import ResearchRunLedger
from strategy_lab.strategy_ideas import seed_strategy_specs


def test_run_backtest_batch_creates_fresh_records_and_ledger_entry(tmp_path) -> None:
    experiment_log = tmp_path / "experiments.jsonl"
    run_log = tmp_path / "runs.jsonl"
    report = tmp_path / "latest.md"

    first = run_backtest_batch(
        experiment_log_path=experiment_log,
        run_log_path=run_log,
        report_path=report,
        purpose="test first batch",
        limit=3,
        synthetic_days=260,
    )
    second = run_backtest_batch(
        experiment_log_path=experiment_log,
        run_log_path=run_log,
        report_path=report,
        purpose="test second batch",
        limit=3,
        synthetic_days=260,
    )

    assert first.experiments_created == 3
    assert second.experiments_created == 3
    assert len(ExperimentLog(experiment_log).records()) == 6
    assert len(ResearchRunLedger(run_log).records()) == 2
    assert report.exists()


def test_out_of_sample_validation_reports_train_and_test_scores() -> None:
    from datetime import date, timedelta

    start = date(2021, 1, 1)
    bars = [
        PriceBar(date=(start + timedelta(days=d)).isoformat(), symbol="SPY", close=100.0 + d * 0.3)
        for d in range(400)
    ]
    strategy = StrategySpec(
        family="mean_reversion",
        name="rsi_pullback",
        hypothesis="",
        rules={},
        parameters={"rsi_period": 14, "entry_rsi": 35, "exit_rsi": 55, "sma_filter": 0},
        risk_model={"max_hold_days": 10},
    )
    validation, weaknesses = out_of_sample_validation(strategy, bars)
    assert validation["status"] in {"evaluated", "inconclusive_few_oos_trades"}
    assert "train_score" in validation
    assert "oos_score" in validation
    assert isinstance(weaknesses, list)


def test_run_backtest_window_keeps_indicators_warm() -> None:
    """A long-SMA-filtered strategy must still trade in a window it could never
    warm up on cold: signals come from the full history, not the slice."""
    from datetime import date, timedelta
    import math

    start = date(2020, 1, 1)
    bars = [
        PriceBar(
            date=(start + timedelta(days=d)).isoformat(),
            symbol="QQQ",
            close=100.0 + d * 0.1 + 8.0 * math.sin(d * 0.25),
        )
        for d in range(600)
    ]
    strategy = StrategySpec(
        family="mean_reversion",
        name="rsi_pullback",
        hypothesis="",
        rules={},
        # sma_filter=200 with a 180-bar window: cold evaluation cannot warm up at
        # all (sma() returns the current close during warmup, blocking entries).
        parameters={"rsi_period": 7, "entry_rsi": 45, "exit_rsi": 60, "sma_filter": 200},
        risk_model={"max_hold_days": 10},
    )
    split = 420  # window = bars[420:600], 180 bars < 200-bar warmup
    from strategy_lab.backtest import run_backtest

    cold = run_backtest(strategy, bars[split:])
    warm = run_backtest_window(strategy, bars, split)
    assert cold["trade_count"] == 0.0  # cold slice never clears the SMA warmup
    assert warm["trade_count"] > 0.0  # warm indicators trade from bar one


def test_heartbeat_fires_once_per_experiment_not_per_symbol(tmp_path) -> None:
    """The lock heartbeat must fire from inside the per-EXPERIMENT loop. A
    single deep-history symbol can run for ~2h, so anything coarser than
    per-experiment cadence would force the stale-lock threshold back up near
    that ceiling (see docs/handoff/DEBUGGING.md D3)."""
    from strategy_lab.models import DatasetSpec

    bars = [
        PriceBar(date=f"2021-{m:02d}-{d:02d}", symbol="SPY", close=100.0 + m + d)
        for m in range(1, 13)
        for d in range(1, 29)
    ]
    strategies = seed_strategy_specs()[:3]
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    dataset = DatasetSpec(name="t", symbols=["SPY"], timeframe="1D", start=bars[0].date, end=bars[-1].date)

    beats = []
    evaluate_and_log_strategies(
        strategies, bars, dataset, log, heartbeat=lambda: beats.append(1)
    )
    assert len(beats) == len(strategies)  # once per experiment, not once total


def test_heartbeat_failure_never_aborts_the_batch(tmp_path) -> None:
    """A raising heartbeat must be swallowed — it is best-effort instrumentation,
    never allowed to break a research batch."""
    from strategy_lab.models import DatasetSpec

    bars = [
        PriceBar(date=f"2021-{m:02d}-{d:02d}", symbol="SPY", close=100.0 + m + d)
        for m in range(1, 13)
        for d in range(1, 29)
    ]
    strategies = seed_strategy_specs()[:2]
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    dataset = DatasetSpec(name="t", symbols=["SPY"], timeframe="1D", start=bars[0].date, end=bars[-1].date)

    def _broken_heartbeat():
        raise OSError("simulated disk hiccup")

    created, skipped, errored, notes = evaluate_and_log_strategies(
        strategies, bars, dataset, log, heartbeat=_broken_heartbeat
    )
    assert created + skipped == len(strategies)  # batch still completed normally


def test_trim_final_exam_reserves_the_tail() -> None:
    bars = [
        PriceBar(date=f"2021-{m:02d}-{d:02d}", symbol="SPY", close=100.0)
        for m in range(1, 13)
        for d in range(1, 29)
    ]  # 336 bars
    trimmed = trim_final_exam(bars)
    assert len(trimmed) == final_exam_start_index(len(bars))
    assert len(trimmed) < len(bars)
    # The trimmed slice is the OLDEST portion — the newest bars are the exam.
    assert trimmed[-1].date < sorted(bars, key=lambda b: b.date)[-1].date

    tiny = bars[:100]  # too small to afford a holdout
    assert len(trim_final_exam(tiny)) == 100


def test_out_of_sample_validation_flags_insufficient_data() -> None:
    bars = [PriceBar(date=f"2021-01-{d:02d}", symbol="SPY", close=100.0 + d) for d in range(1, 20)]
    strategy = StrategySpec(
        family="mean_reversion",
        name="rsi_pullback",
        hypothesis="",
        rules={},
        parameters={"rsi_period": 14, "entry_rsi": 35, "exit_rsi": 55, "sma_filter": 0},
        risk_model={"max_hold_days": 10},
    )
    validation, weaknesses = out_of_sample_validation(strategy, bars)
    assert validation["status"] == "insufficient_data"
    assert weaknesses == []


def test_run_backtest_batch_can_run_distinct_shards(tmp_path) -> None:
    first_log = tmp_path / "experiments_0.jsonl"
    second_log = tmp_path / "experiments_1.jsonl"

    run_backtest_batch(
        experiment_log_path=first_log,
        run_log_path=tmp_path / "runs_0.jsonl",
        report_path=tmp_path / "latest_0.md",
        purpose="test shard 0",
        limit=5,
        synthetic_days=260,
        shard_index=0,
        shard_count=2,
    )
    run_backtest_batch(
        experiment_log_path=second_log,
        run_log_path=tmp_path / "runs_1.jsonl",
        report_path=tmp_path / "latest_1.md",
        purpose="test shard 1",
        limit=5,
        synthetic_days=260,
        shard_index=1,
        shard_count=2,
    )

    fingerprints_0 = ExperimentLog(first_log).fingerprints()
    fingerprints_1 = ExperimentLog(second_log).fingerprints()
    assert fingerprints_0
    assert fingerprints_1
    assert fingerprints_0.isdisjoint(fingerprints_1)
