"""
Autonomous research loop — a bounded parameter hill-climber.

Instead of expanding grids blindly (which explodes combinatorially and invites
overfitting), this refines *around the current best, out-of-sample-validated*
strategies: take the top robust results, perturb each tunable parameter by a
small step, backtest the neighbours, and keep whatever the existing OOS gate and
scoring accept. Run on a schedule, it keeps nudging the frontier on its own.

Guardrails that keep it safe (not a data-snooping machine):
  - only refines combos that already passed out-of-sample validation
  - every neighbour is itself OOS-gated and stability-scored on the way in
  - bounded per round (top_k seeds x small perturbations, capped at max_new)
  - never writes code or grids; only adds data-driven experiments
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from strategy_lab.analysis import cross_symbol_support, top_robust_records
from strategy_lab.batch_runner import (
    MIN_OOS_TRADES,
    OOS_FAIL_SCORE,
    evaluate_and_log_strategies,
)
from strategy_lab.data_loader import load_price_bars_from_csv
from strategy_lab.experiment_log import ExperimentLog
from strategy_lab.fingerprints import experiment_fingerprint
from strategy_lab.models import StrategySpec
from strategy_lab.reporting import write_markdown_report
from strategy_lab.run_ledger import ResearchRunLedger, ResearchRunRecord

# Semantic bounds for parameters whose valid range the perturber cannot infer
# from the value alone. RSI lives on 0-100: without a ceiling the climber walks
# exit_rsi past 100, where the exit can never fire and max_hold becomes the only
# exit — a nonsense strategy that can still score well by accident.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "entry_rsi": (1, 99),
    "exit_rsi": (1, 99),
}

# At most this many seeds per strategy family within one symbol. Without a cap,
# one hot corner floods the whole seed pool with its own children (each raising
# its parent's stability score), and every other family stops being explored.
MAX_SEEDS_PER_FAMILY = 2


def _perturb(value):
    """Candidate neighbour values one step away from a numeric parameter."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        step = max(1, round(abs(value) * 0.15))
        return [value - step, value + step]
    if isinstance(value, float):
        return [round(value * 0.85, 4), round(value * 1.15, 4)]
    return []


def _eligible_seed(record: dict) -> bool:
    """
    A seed must have genuinely survived out-of-sample validation. Ranking alone
    is not enough: top_robust_records ranks by in-sample score, and refining an
    OOS-unproven record turns the loop into an in-sample overfitting machine.
    """
    if record.get("grade") == "reject":
        return False
    validation = record.get("validation") or {}
    if validation.get("status") != "evaluated":
        return False
    oos_score = validation.get("oos_score")
    oos_trades = validation.get("oos_trade_count")
    return (
        oos_score is not None
        and oos_score >= OOS_FAIL_SCORE
        and (oos_trades or 0) >= MIN_OOS_TRADES
    )


def _seed_rank_key(record: dict) -> float:
    """
    Rank by the WEAKER of in-sample stability and out-of-sample score. Ranking
    on in-sample alone is what let exit_rsi drift 65→75→86: each round the
    climber maximised the full-sample fit while the OOS number stood still.
    A seed is only as good as its worst evidence.
    """
    validation = record.get("validation") or {}
    oos = validation.get("oos_score") or 0.0
    return min(record.get("stability_score", record["score"]), oos)


def select_seeds(
    scoped_records: list[dict],
    top_k: int,
    symbol_support: dict[str, int] | None = None,
) -> list[dict]:
    """
    OOS-eligible, family-diverse seeds ranked by their weakest evidence, with
    cross-symbol confirmation as the leading sort key: a combo that also holds
    up on sibling symbols outranks an equally-scored single-symbol result,
    because symbol-specific flukes rarely replicate across instruments.
    """
    support = symbol_support or {}
    pool = top_robust_records(scoped_records, limit=top_k * 5)
    eligible = [record for record in pool if _eligible_seed(record)]
    eligible.sort(
        key=lambda record: (
            support.get(record.get("fingerprint"), 0) > 0,
            _seed_rank_key(record),
        ),
        reverse=True,
    )

    seeds: list[dict] = []
    per_family: dict[str, int] = {}
    for record in eligible:
        name = record["strategy"]["name"]
        if per_family.get(name, 0) >= MAX_SEEDS_PER_FAMILY:
            continue
        per_family[name] = per_family.get(name, 0) + 1
        seeds.append(record)
        if len(seeds) >= top_k:
            break
    return seeds


def _within_bounds(key: str, candidate) -> bool:
    bounds = PARAM_BOUNDS.get(key)
    if bounds is None:
        return True
    low, high = bounds
    return low <= candidate <= high


def propose_refinements(
    records: list[dict],
    dataset,
    existing_fingerprints: set[str],
    *,
    top_k: int = 6,
    max_new: int = 60,
) -> list[StrategySpec]:
    """Neighbour specs around the top OOS-validated results for this symbol."""
    symbol = (dataset.symbols or ["?"])[0]
    # Cross-symbol support must be computed on the FULL record set (all symbols)
    # before scoping, or no sibling evidence would ever be visible.
    support = cross_symbol_support(records)
    scoped = [r for r in records if (r["dataset"].get("symbols") or ["?"])[0] == symbol]
    seeds = select_seeds(scoped, top_k, symbol_support=support)

    proposals: list[StrategySpec] = []
    for seed in seeds:
        strategy = seed["strategy"]
        tunables = {**strategy.get("parameters", {}), **strategy.get("risk_model", {})}
        for key, value in tunables.items():
            for candidate in _perturb(value):
                if isinstance(candidate, (int, float)) and candidate <= 0:
                    continue  # negative thresholds/periods are nonsense
                if not _within_bounds(key, candidate):
                    continue
                parameters = dict(strategy.get("parameters", {}))
                risk_model = dict(strategy.get("risk_model", {}))
                if key in parameters:
                    parameters[key] = candidate
                else:
                    risk_model[key] = candidate
                spec = StrategySpec(
                    family=strategy["family"],
                    name=strategy["name"],
                    hypothesis=strategy.get("hypothesis", ""),
                    rules=strategy.get("rules", {}),
                    parameters=parameters,
                    risk_model=risk_model,
                )
                fingerprint = experiment_fingerprint(spec, dataset)
                if fingerprint in existing_fingerprints:
                    continue
                existing_fingerprints.add(fingerprint)
                proposals.append(spec)
                if len(proposals) >= max_new:
                    return proposals
    return proposals


def run_auto_research(
    *,
    experiment_log_path: Path,
    run_log_path: Path,
    report_path: Path,
    data_csv: Path,
    symbols: list[str],
    top_k: int = 6,
    max_new_per_symbol: int = 60,
) -> dict:
    """One refinement round across all symbols. Returns a summary dict."""
    experiment_log = ExperimentLog(experiment_log_path)
    records = experiment_log.records()
    known = experiment_log.fingerprints()

    per_symbol = {}
    total_created = 0
    best_before = max((r["score"] for r in records), default=0.0)

    for symbol in symbols:
        bars, dataset = load_price_bars_from_csv(data_csv, symbol)
        proposals = propose_refinements(
            records, dataset, known, top_k=top_k, max_new=max_new_per_symbol
        )
        created, _, errored, _ = evaluate_and_log_strategies(
            proposals, bars, dataset, experiment_log
        )
        total_created += created
        per_symbol[symbol] = {"proposed": len(proposals), "created": created, "errored": errored}

    all_records = experiment_log.records()
    write_markdown_report(all_records, report_path)
    best_after = max((r["score"] for r in all_records), default=0.0)
    grades = Counter(r.get("grade") for r in all_records)

    ResearchRunLedger(run_log_path).append(
        ResearchRunRecord(
            purpose="auto-research refinement round",
            mode="auto_research",
            status="completed",
            experiment_log_path=str(experiment_log_path),
            report_path=str(report_path),
            experiments_attempted=sum(s["proposed"] for s in per_symbol.values()),
            experiments_created=total_created,
            experiments_skipped_duplicates=0,
            strategy_families={},
            grade_counts=dict(grades),
            next_action="Review refined results; run another round if the frontier improved.",
            notes=[f"{sym}: {info}" for sym, info in per_symbol.items()],
        )
    )

    return {
        "experiments_created": total_created,
        "best_score_before": round(best_before, 2),
        "best_score_after": round(best_after, 2),
        "improved": best_after > best_before + 0.01,
        "per_symbol": per_symbol,
        "promising": grades.get("promising", 0),
        "candidates": grades.get("candidate", 0),
    }
