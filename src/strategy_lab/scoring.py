from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CRITERIA_PATH = Path(__file__).resolve().parents[2] / "configs" / "research_criteria.yaml"


@dataclass(frozen=True)
class ScoreResult:
    score: float
    grade: str
    weaknesses: list[str]


def load_criteria(path: Path | str = DEFAULT_CRITERIA_PATH) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def score_metrics(
    metrics: dict[str, float],
    criteria: dict[str, Any] | None = None,
) -> ScoreResult:
    active_criteria = criteria or load_criteria()
    weaknesses = hard_reject_weaknesses(metrics, active_criteria)
    score = weighted_score(metrics, active_criteria)
    grade = grade_for_score(score, active_criteria)
    if weaknesses:
        grade = "reject"
    return ScoreResult(score=round(score, 2), grade=grade, weaknesses=weaknesses)


def hard_reject_weaknesses(
    metrics: dict[str, float],
    criteria: dict[str, Any],
) -> list[str]:
    hard_rejects = criteria.get("hard_rejects", {})
    weaknesses: list[str] = []

    drawdown_limit = hard_rejects.get("max_drawdown_pct_above")
    if drawdown_limit is not None and metrics.get("max_drawdown_pct", 0) > drawdown_limit:
        weaknesses.append(f"max_drawdown_pct above {drawdown_limit}")

    trade_floor = hard_rejects.get("trade_count_below")
    if trade_floor is not None and metrics.get("trade_count", 0) < trade_floor:
        weaknesses.append(f"trade_count below {trade_floor}")

    profit_factor_floor = hard_rejects.get("profit_factor_below")
    if (
        profit_factor_floor is not None
        and metrics.get("profit_factor", 0) < profit_factor_floor
    ):
        weaknesses.append(f"profit_factor below {profit_factor_floor}")

    return weaknesses


def weighted_score(metrics: dict[str, float], criteria: dict[str, Any]) -> float:
    weights = criteria["weights"]
    targets = criteria["metric_targets"]
    total = 0.0

    for metric_name, weight in weights.items():
        value = metrics.get(metric_name)
        if value is None:
            continue
        target = targets[metric_name]
        total += normalize_metric(metric_name, value, target) * weight

    return max(0.0, min(100.0, total * 100.0))


def normalize_metric(metric_name: str, value: float, target: dict[str, float]) -> float:
    poor = float(target["poor"])
    excellent = float(target["excellent"])

    if metric_name in {"max_drawdown_pct", "exposure_pct"}:
        normalized = (poor - value) / (poor - excellent)
    else:
        normalized = (value - poor) / (excellent - poor)

    return max(0.0, min(1.0, normalized))


def grade_for_score(score: float, criteria: dict[str, Any]) -> str:
    thresholds = criteria["grade_thresholds"]
    if score >= thresholds["candidate"]:
        return "candidate"
    if score >= thresholds["promising"]:
        return "promising"
    if score >= thresholds["watch"]:
        return "watch"
    return "reject"

