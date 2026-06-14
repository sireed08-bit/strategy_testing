from strategy_lab.run_ledger import ResearchRunLedger, ResearchRunRecord


def test_research_run_ledger_appends_and_reads_latest(tmp_path) -> None:
    ledger = ResearchRunLedger(tmp_path / "runs.jsonl")
    record = ResearchRunRecord(
        purpose="Test run ledger.",
        mode="seed_batch",
        status="completed",
        experiment_log_path="experiments.jsonl",
        report_path="latest.md",
        experiments_attempted=7,
        experiments_created=7,
        experiments_skipped_duplicates=0,
        strategy_families={"trend_following": 1},
        grade_counts={"watch": 7},
        next_action="Run real backtests.",
    )

    ledger.append(record)

    latest = ledger.latest()
    assert latest is not None
    assert latest["run_id"] == record.run_id
    assert latest["experiments_created"] == 7
    assert latest["next_action"] == "Run real backtests."

