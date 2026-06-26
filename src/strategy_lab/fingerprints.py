from __future__ import annotations

import hashlib
import json
from typing import Any

from strategy_lab.models import DatasetSpec, StrategySpec

# Bump whenever the backtest engine's semantics change (e.g. a risk_model
# parameter becomes enforced). This invalidates every prior fingerprint so the
# affected experiments re-run instead of being skipped as stale duplicates.
#   v1: initial engine; risk_model present but never enforced.
#   v2: max_hold_days enforced in rsi_pullback; fingerprint covers only
#       computation-relevant fields (name/parameters/risk_model), not prose.
ENGINE_VERSION = 2


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def experiment_fingerprint(strategy: StrategySpec, dataset: DatasetSpec) -> str:
    # Hash only inputs that affect the computed result. hypothesis/rules are
    # descriptive prose with no effect on the backtest — including them would
    # silently orphan the entire log whenever a description is reworded.
    payload = {
        "engine_version": ENGINE_VERSION,
        "name": strategy.name,
        "parameters": strategy.parameters,
        "risk_model": strategy.risk_model,
        "dataset": dataset.to_dict(),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

