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
#   v3: realistic execution — T+1 entry and per-side transaction costs — plus
#       out-of-sample validation gating the grade. Results are net of costs.
#   v4: out-of-sample failure keyed on the held-out *score*, not the OOS grade
#       (whose trade_count hard-reject was miscalibrated for the short window and
#       wrongly failed strategies that actually generalised).
#   v5: realistic stop-loss fills (stop price, or the open on a gap-through —
#       never the recovered close), warm-indicator out-of-sample evaluation, and
#       a final-exam holdout tail excluded from all optimisation.
#   v6: benchmark-relative metrics — benchmark_return_pct and excess_return_pct
#       (strategy minus buy-and-hold on the same bars) join the metric set and
#       carry scoring weight. A strategy that loses to holding is not a finding.
ENGINE_VERSION = 6


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

