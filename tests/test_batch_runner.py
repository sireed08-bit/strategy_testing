from strategy_lab.backtest import PriceBar
from strategy_lab.batch_runner import out_of_sample_validation, run_backtest_batch
from strategy_lab.experiment_log import ExperimentLog
from strategy_lab.models import StrategySpec
from strategy_lab.run_ledger import ResearchRunLedger


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
