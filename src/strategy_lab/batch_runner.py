from __future__ import annotations

from collections import Counter
from pathlib import Path

from strategy_lab.backtest import run_backtest
from strategy_lab.data_loader import load_price_bars_from_csv, synthetic_price_bars
from strategy_lab.experiment_generator import fresh_strategy_variations
from strategy_lab.experiment_log import DuplicateExperimentError, ExperimentLog
from strategy_lab.fingerprints import experiment_fingerprint
from strategy_lab.models import ExperimentRecord
from strategy_lab.reporting import write_markdown_report
from strategy_lab.run_ledger import ResearchRunLedger, ResearchRunRecord
from strategy_lab.scoring import score_metrics


def run_backtest_batch(
    *,
    experiment_log_path: Path,
    run_log_path: Path,
    report_path: Path,
    purpose: str,
    limit: int = 20,
    data_csv: Path | None = None,
    symbol: str = "SPY",
    synthetic_days: int = 756,
    shard_index: int = 0,
    shard_count: int = 1,
) -> ResearchRunRecord:
    bars, dataset = (
        load_price_bars_from_csv(data_csv, symbol)
        if data_csv
        else synthetic_price_bars(symbol=symbol, days=synthetic_days)
    )
    experiment_log = ExperimentLog(experiment_log_path)
    strategies = fresh_strategy_variations(
        dataset=dataset,
        experiment_log=experiment_log,
        limit=limit,
        shard_index=shard_index,
        shard_count=shard_count,
    )

    created = 0
    skipped = 0
    errored = 0
    notes: list[str] = []
    for strategy in strategies:
        try:
            metrics = run_backtest(strategy, bars)
            result = score_metrics(metrics)
            record = ExperimentRecord(
                strategy=strategy,
                dataset=dataset,
                metrics=metrics,
                score=result.score,
                grade=result.grade,
                conclusion=conclusion_for_grade(result.grade),
                fingerprint=experiment_fingerprint(strategy, dataset),
                weaknesses=result.weaknesses,
                next_action=next_action_for_grade(result.grade),
            )
            experiment_log.append(record)
            created += 1
        except DuplicateExperimentError:
            skipped += 1
        except ValueError as exc:
            errored += 1
            notes.append(f"{strategy.family}/{strategy.name}: {exc}")

    all_records = experiment_log.records()
    write_markdown_report(all_records, report_path)
    run_record = ResearchRunRecord(
        purpose=purpose,
        mode="backtest_batch",
        status="completed" if errored == 0 else "completed_with_errors",
        experiment_log_path=str(experiment_log_path),
        report_path=str(report_path),
        experiments_attempted=len(strategies),
        experiments_created=created,
        experiments_skipped_duplicates=skipped,
        strategy_families=dict(Counter(strategy.family for strategy in strategies)),
        grade_counts=dict(Counter(record.get("grade", "unknown") for record in all_records)),
        next_action=next_action_for_batch(created, errored),
        notes=notes
        + [
            f"dataset={dataset.name}",
            f"symbol={symbol}",
            f"shard={shard_index}/{shard_count}",
            "Synthetic data is for plumbing validation only." if data_csv is None else "External CSV data supplied.",
        ],
        artifacts={
            "experiment_log": str(experiment_log_path),
            "report": str(report_path),
        },
    )
    ResearchRunLedger(run_log_path).append(run_record)
    return run_record


def conclusion_for_grade(grade: str) -> str:
    if grade == "candidate":
        return "Candidate result; promote to robustness checks before paper observation."
    if grade == "promising":
        return "Promising result; expand nearby parameter and symbol robustness tests."
    if grade == "watch":
        return "Watchlist result; keep for comparison and revisit if related branches improve."
    return "Rejected by current scoring criteria."


def next_action_for_grade(grade: str) -> str:
    if grade in {"candidate", "promising"}:
        return "Run robustness checks across additional symbols and periods."
    if grade == "watch":
        return "Compare against related variations before revisiting."
    return "Do not revisit unless data, rules, or hypothesis changes."


def next_action_for_batch(created: int, errored: int) -> str:
    if created == 0 and errored == 0:
        return "Experiment space exhausted for this dataset; expand grids or add strategy families."
    if errored:
        return "Fix errored strategy implementations, then rerun the batch."
    return "Review report, then run the next fresh batch or add real historical data."
