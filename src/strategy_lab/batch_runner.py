from __future__ import annotations

from collections import Counter
from pathlib import Path

from strategy_lab.backtest import PriceBar, run_backtest
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
            validation, oos_weaknesses = out_of_sample_validation(strategy, bars)
            grade = result.grade
            weaknesses = list(result.weaknesses) + oos_weaknesses
            # Out-of-sample failure demotes the grade: a result that looks good on
            # the full history but collapses on held-out data is overfit, not real.
            if oos_weaknesses:
                if any("fails out-of-sample" in w for w in oos_weaknesses):
                    grade = "reject"
                else:
                    grade = _demote_to_watch(grade)
            record = ExperimentRecord(
                strategy=strategy,
                dataset=dataset,
                metrics=metrics,
                score=result.score,
                grade=grade,
                conclusion=conclusion_for_grade(grade),
                fingerprint=experiment_fingerprint(strategy, dataset),
                weaknesses=weaknesses,
                next_action=next_action_for_grade(grade),
                validation=validation,
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


# ── out-of-sample validation ──────────────────────────────────────────────────
# Optimize/observe on the first TRAIN_FRACTION of history, then judge on the
# held-out remainder the parameter scan never saw. This is the primary defence
# against data-snooping: with thousands of parameter sets scored on one price
# path, the top scorers are likely lucky fits unless they survive unseen data.
TRAIN_FRACTION = 0.7
MIN_OOS_TRADES = 5          # below this, the test window is too quiet to judge
MAX_OOS_DEGRADATION = 25.0  # in-sample minus out-of-sample score points
OOS_FAIL_SCORE = 45.0       # held-out score below this = the edge did not generalise
_MIN_SEGMENT_BARS = 60


def out_of_sample_validation(
    strategy: StrategySpec,
    bars: list[PriceBar],
) -> tuple[dict, list[str]]:
    ordered = sorted(bars, key=lambda bar: bar.date)
    split = int(len(ordered) * TRAIN_FRACTION)
    train, test = ordered[:split], ordered[split:]
    if len(train) < _MIN_SEGMENT_BARS or len(test) < _MIN_SEGMENT_BARS:
        return {"status": "insufficient_data"}, []

    train_score = score_metrics(run_backtest(strategy, train)).score
    oos_metrics = run_backtest(strategy, test)
    oos = score_metrics(oos_metrics)
    degradation = round(train_score - oos.score, 2)

    validation = {
        "status": "evaluated",
        "train_frac": TRAIN_FRACTION,
        "train_score": train_score,
        "oos_score": oos.score,
        "oos_grade": oos.grade,
        "degradation": degradation,
        "oos_trade_count": oos_metrics["trade_count"],
        "oos_max_drawdown_pct": oos_metrics["max_drawdown_pct"],
    }

    weaknesses: list[str] = []
    if oos_metrics["trade_count"] < MIN_OOS_TRADES:
        # The held-out window is too quiet to judge — not a failure, just thin.
        # (Judging by oos.grade here would be wrong: the full-history trade_count
        # hard-reject is miscalibrated for a 30% window and would fail strategies
        # that actually generalise.)
        validation["status"] = "inconclusive_few_oos_trades"
    elif oos.score < OOS_FAIL_SCORE:
        weaknesses.append(f"fails out-of-sample (oos_score={oos.score})")
    elif degradation > MAX_OOS_DEGRADATION:
        weaknesses.append(f"unstable out-of-sample (is/oos gap={degradation})")

    return validation, weaknesses


def _demote_to_watch(grade: str) -> str:
    return "watch" if grade in {"promising", "candidate"} else grade


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
