from strategy_lab.batch_runner import run_backtest_batch
from strategy_lab.experiment_log import ExperimentLog
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
