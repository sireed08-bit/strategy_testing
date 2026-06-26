from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any

import yaml

from strategy_lab.experiment_log import ExperimentLog
from strategy_lab.fingerprints import experiment_fingerprint
from strategy_lab.models import DatasetSpec, StrategySpec


DEFAULT_EXPERIMENT_SPACE_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "experiment_space.yaml"
)


def load_experiment_space(path: Path | str = DEFAULT_EXPERIMENT_SPACE_PATH) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def generate_strategy_variations(
    experiment_space: dict[str, Any] | None = None,
) -> list[StrategySpec]:
    active_space = experiment_space or load_experiment_space()
    variations: list[StrategySpec] = []
    for item in active_space["strategies"]:
        parameter_grid = item.get("parameter_grid", {})
        risk_grid = item.get("risk_grid", {})
        for parameters in expand_grid(parameter_grid):
            if not passes_constraints(parameters, item.get("constraints", [])):
                continue
            for risk_model in expand_grid(risk_grid):
                variations.append(
                    StrategySpec(
                        family=item["family"],
                        name=item["name"],
                        hypothesis=item["hypothesis"],
                        rules=item["rules"],
                        parameters=parameters,
                        risk_model=risk_model,
                    )
                )
    return variations


def fresh_strategy_variations(
    *,
    dataset: DatasetSpec,
    experiment_log: ExperimentLog,
    limit: int,
    shard_index: int = 0,
    shard_count: int = 1,
    experiment_space: dict[str, Any] | None = None,
) -> list[StrategySpec]:
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("shard_index must be between 0 and shard_count - 1")

    seen = experiment_log.fingerprints()
    fresh: list[StrategySpec] = []
    for index, strategy in enumerate(generate_strategy_variations(experiment_space)):
        if index % shard_count != shard_index:
            continue
        fingerprint = experiment_fingerprint(strategy, dataset)
        if fingerprint in seen:
            continue
        fresh.append(strategy)
        if len(fresh) >= limit:
            break
    return fresh


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [
        dict(zip(keys, values, strict=True))
        for values in product(*(grid[key] for key in keys))
    ]


def passes_constraints(parameters: dict[str, Any], constraints: list[str]) -> bool:
    for constraint in constraints:
        if constraint == "fast_sma_lt_slow_sma":
            if int(parameters["fast_sma"]) >= int(parameters["slow_sma"]):
                return False
        elif constraint == "exit_lookback_lt_entry_lookback":
            if int(parameters["exit_lookback"]) >= int(parameters["entry_lookback"]):
                return False
        else:
            raise ValueError(f"Unknown experiment constraint: {constraint}")
    return True
