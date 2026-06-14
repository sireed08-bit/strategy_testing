from __future__ import annotations

import hashlib
import json
from typing import Any

from strategy_lab.models import DatasetSpec, StrategySpec


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def experiment_fingerprint(strategy: StrategySpec, dataset: DatasetSpec) -> str:
    payload = {
        "strategy": strategy.to_dict(),
        "dataset": dataset.to_dict(),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

