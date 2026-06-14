from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from collections import Counter

from strategy_lab.alpaca_data import download_stock_bars_csv
from strategy_lab.backtest import PriceBar, run_backtest
from strategy_lab.batch_runner import run_backtest_batch
from strategy_lab.encrypted_bundle import decrypt_file, encrypt_file, passphrase_from_env
from strategy_lab.experiment_log import DuplicateExperimentError, ExperimentLog
from strategy_lab.handoff import BatchRequest, create_result_bundle, write_batch_request
from strategy_lab.jsonl_merge import merge_jsonl_files
from strategy_lab.private_storage import initialize_private_storage
from strategy_lab.reporting import write_markdown_report
from strategy_lab.run_ledger import ResearchRunLedger, ResearchRunRecord
from strategy_lab.scoring import score_metrics
from strategy_lab.strategy_ideas import placeholder_records, seed_strategy_specs


def main() -> None:
    parser = argparse.ArgumentParser(prog="strategy-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--log", required=True, help="Experiment JSONL path")

    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--metrics", required=True, help="Metrics JSON path")

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--log", required=True, help="Experiment JSONL path")
    report_parser.add_argument("--output", required=True, help="Markdown report path")

    batch_parser = subparsers.add_parser("run-seed-batch")
    batch_parser.add_argument(
        "--experiment-log",
        default="data/experiments/experiment_log.jsonl",
        help="Experiment JSONL path",
    )
    batch_parser.add_argument(
        "--run-log",
        default="data/runs/research_runs.jsonl",
        help="Research run ledger JSONL path",
    )
    batch_parser.add_argument(
        "--report",
        default="reports/latest.md",
        help="Markdown report path",
    )
    batch_parser.add_argument(
        "--purpose",
        default="Seed initial strategy family research queue.",
        help="Human-readable reason for this run",
    )

    backtest_batch_parser = subparsers.add_parser("run-backtest-batch")
    backtest_batch_parser.add_argument(
        "--experiment-log",
        default="data/experiments/experiment_log.jsonl",
        help="Experiment JSONL path",
    )
    backtest_batch_parser.add_argument(
        "--run-log",
        default="data/runs/research_runs.jsonl",
        help="Research run ledger JSONL path",
    )
    backtest_batch_parser.add_argument(
        "--report",
        default="reports/latest.md",
        help="Markdown report path",
    )
    backtest_batch_parser.add_argument(
        "--purpose",
        default="Run a fresh strategy variation backtest batch.",
        help="Human-readable reason for this run",
    )
    backtest_batch_parser.add_argument("--limit", type=int, default=20)
    backtest_batch_parser.add_argument("--data-csv")
    backtest_batch_parser.add_argument("--symbol", default="SPY")
    backtest_batch_parser.add_argument("--synthetic-days", type=int, default=756)
    backtest_batch_parser.add_argument("--shard-index", type=int, default=0)
    backtest_batch_parser.add_argument("--shard-count", type=int, default=1)

    merge_parser = subparsers.add_parser("merge-jsonl")
    merge_parser.add_argument("--output", required=True)
    merge_parser.add_argument("--unique-key", required=True)
    merge_parser.add_argument("inputs", nargs="+")

    storage_parser = subparsers.add_parser("init-private-storage")
    storage_parser.add_argument("--root", required=True)

    alpaca_parser = subparsers.add_parser("download-alpaca-bars")
    alpaca_parser.add_argument("--symbols", required=True)
    alpaca_parser.add_argument("--start", required=True)
    alpaca_parser.add_argument("--end", required=True)
    alpaca_parser.add_argument("--output", required=True)
    alpaca_parser.add_argument("--timeframe", default="1Day")
    alpaca_parser.add_argument("--feed", default="iex")

    request_parser = subparsers.add_parser("write-batch-request")
    request_parser.add_argument("--output", required=True)
    request_parser.add_argument("--purpose", required=True)
    request_parser.add_argument("--strategy-names", required=True)
    request_parser.add_argument("--symbols", required=True)
    request_parser.add_argument("--dataset-name", required=True)
    request_parser.add_argument("--max-experiments", type=int, default=40)
    request_parser.add_argument("--shard-count", type=int, default=4)

    bundle_parser = subparsers.add_parser("create-result-bundle")
    bundle_parser.add_argument("--output", required=True)
    bundle_parser.add_argument("--manifest", required=True)
    bundle_parser.add_argument("inputs", nargs="+")

    encrypt_parser = subparsers.add_parser("encrypt-file")
    encrypt_parser.add_argument("--input", required=True)
    encrypt_parser.add_argument("--output", required=True)
    encrypt_parser.add_argument(
        "--passphrase-env",
        default="STRATEGY_BUNDLE_PASSPHRASE",
        help="Environment variable containing the encryption passphrase",
    )

    decrypt_parser = subparsers.add_parser("decrypt-file")
    decrypt_parser.add_argument("--input", required=True)
    decrypt_parser.add_argument("--output", required=True)
    decrypt_parser.add_argument(
        "--passphrase-env",
        default="STRATEGY_BUNDLE_PASSPHRASE",
        help="Environment variable containing the decryption passphrase",
    )

    subparsers.add_parser("backtest-sample")

    args = parser.parse_args()
    if args.command == "seed":
        seed(Path(args.log))
    elif args.command == "score":
        score(Path(args.metrics))
    elif args.command == "report":
        report(Path(args.log), Path(args.output))
    elif args.command == "run-seed-batch":
        run_seed_batch(
            experiment_log_path=Path(args.experiment_log),
            run_log_path=Path(args.run_log),
            report_path=Path(args.report),
            purpose=args.purpose,
        )
    elif args.command == "run-backtest-batch":
        run_record = run_backtest_batch(
            experiment_log_path=Path(args.experiment_log),
            run_log_path=Path(args.run_log),
            report_path=Path(args.report),
            purpose=args.purpose,
            limit=args.limit,
            data_csv=Path(args.data_csv) if args.data_csv else None,
            symbol=args.symbol,
            synthetic_days=args.synthetic_days,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
        print(json.dumps(run_record.to_dict(), indent=2))
    elif args.command == "merge-jsonl":
        merged = merge_jsonl_files(
            input_paths=[Path(item) for item in args.inputs],
            output_path=Path(args.output),
            unique_key=args.unique_key,
        )
        print(json.dumps({"merged": merged, "output": args.output}))
    elif args.command == "init-private-storage":
        layout = initialize_private_storage(Path(args.root))
        print(
            json.dumps(
                {
                    "root": str(layout.root),
                    "directories": [str(item) for item in layout.directories],
                },
                indent=2,
            )
        )
    elif args.command == "download-alpaca-bars":
        rows = download_stock_bars_csv(
            symbols=parse_csv_arg(args.symbols),
            start=args.start,
            end=args.end,
            output_path=Path(args.output),
            timeframe=args.timeframe,
            feed=args.feed,
        )
        print(json.dumps({"rows": rows, "output": args.output}))
    elif args.command == "write-batch-request":
        request = BatchRequest(
            purpose=args.purpose,
            strategy_names=parse_csv_arg(args.strategy_names),
            symbols=parse_csv_arg(args.symbols),
            dataset_name=args.dataset_name,
            max_experiments=args.max_experiments,
            shard_count=args.shard_count,
        )
        written = write_batch_request(request, args.output)
        print(json.dumps({"request_id": request.request_id, "output": str(written)}))
    elif args.command == "create-result-bundle":
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
        written = create_result_bundle(
            input_paths=[Path(item) for item in args.inputs],
            output_path=Path(args.output),
            manifest=manifest,
        )
        print(json.dumps({"bundle": str(written)}))
    elif args.command == "encrypt-file":
        written = encrypt_file(
            input_path=Path(args.input),
            output_path=Path(args.output),
            passphrase=passphrase_from_env(args.passphrase_env),
        )
        print(json.dumps({"encrypted": str(written)}))
    elif args.command == "decrypt-file":
        written = decrypt_file(
            input_path=Path(args.input),
            output_path=Path(args.output),
            passphrase=passphrase_from_env(args.passphrase_env),
        )
        print(json.dumps({"decrypted": str(written)}))
    elif args.command == "backtest-sample":
        backtest_sample()


def seed(log_path: Path) -> None:
    log = ExperimentLog(log_path)
    created = 0
    skipped = 0
    for record in placeholder_records():
        try:
            log.append(record)
            created += 1
        except DuplicateExperimentError:
            skipped += 1
    print(json.dumps({"created": created, "skipped_duplicates": skipped}))


def score(metrics_path: Path) -> None:
    with metrics_path.open("r", encoding="utf-8") as handle:
        metrics = json.load(handle)
    result = score_metrics(metrics)
    print(
        json.dumps(
            {
                "score": result.score,
                "grade": result.grade,
                "weaknesses": result.weaknesses,
            },
            indent=2,
        )
    )


def report(log_path: Path, output_path: Path) -> None:
    log = ExperimentLog(log_path)
    written = write_markdown_report(log.records(), output_path)
    print(json.dumps({"report": str(written)}))


def run_seed_batch(
    experiment_log_path: Path,
    run_log_path: Path,
    report_path: Path,
    purpose: str,
) -> None:
    experiment_log = ExperimentLog(experiment_log_path)
    records = placeholder_records()
    created = 0
    skipped = 0
    for record in records:
        try:
            experiment_log.append(record)
            created += 1
        except DuplicateExperimentError:
            skipped += 1

    all_records = experiment_log.records()
    write_markdown_report(all_records, report_path)
    run_record = ResearchRunRecord(
        purpose=purpose,
        mode="seed_batch",
        status="completed",
        experiment_log_path=str(experiment_log_path),
        report_path=str(report_path),
        experiments_attempted=len(records),
        experiments_created=created,
        experiments_skipped_duplicates=skipped,
        strategy_families=dict(
            Counter(record.strategy.family for record in records)
        ),
        grade_counts=dict(Counter(record.get("grade", "unknown") for record in all_records)),
        next_action=(
            "Replace seed placeholders with real backtest results for the first "
            "two implemented strategies, then add a batch runner."
        ),
        notes=[
            "Seed records are research queue entries, not completed backtests.",
            "Duplicate fingerprints are skipped to preserve experiment memory.",
        ],
        artifacts={
            "experiment_log": str(experiment_log_path),
            "report": str(report_path),
        },
    )
    ResearchRunLedger(run_log_path).append(run_record)
    print(
        json.dumps(
            {
                "run_id": run_record.run_id,
                "experiments_attempted": run_record.experiments_attempted,
                "experiments_created": run_record.experiments_created,
                "experiments_skipped_duplicates": run_record.experiments_skipped_duplicates,
                "run_log": str(run_log_path),
                "experiment_log": str(experiment_log_path),
                "report": str(report_path),
                "next_action": run_record.next_action,
            },
            indent=2,
        )
    )


def backtest_sample() -> None:
    strategy = seed_strategy_specs()[0]
    start = date(2025, 1, 1)
    bars = [
        PriceBar(
            date=(start + timedelta(days=day)).isoformat(),
            symbol="SPY",
            close=100.0 + day * 0.5,
        )
        for day in range(260)
    ]
    metrics = run_backtest(strategy, bars)
    result = score_metrics(metrics)
    print(
        json.dumps(
            {
                "strategy": strategy.name,
                "metrics": metrics,
                "score": result.score,
                "grade": result.grade,
                "weaknesses": result.weaknesses,
            },
            indent=2,
        )
    )


def parse_csv_arg(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    main()
