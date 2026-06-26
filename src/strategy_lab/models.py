from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class StrategySpec:
    family: str
    name: str
    hypothesis: str
    rules: dict[str, Any]
    parameters: dict[str, Any]
    risk_model: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    symbols: list[str]
    timeframe: str
    start: str
    end: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExperimentRecord:
    strategy: StrategySpec
    dataset: DatasetSpec
    metrics: dict[str, float]
    score: float
    grade: str
    conclusion: str
    fingerprint: str
    weaknesses: list[str] = field(default_factory=list)
    next_action: str = ""
    validation: dict[str, Any] = field(default_factory=dict)
    revisit: dict[str, Any] = field(default_factory=lambda: {"allowed": False})
    experiment_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "strategy": self.strategy.to_dict(),
            "dataset": self.dataset.to_dict(),
            "metrics": self.metrics,
            "score": self.score,
            "grade": self.grade,
            "conclusion": self.conclusion,
            "weaknesses": self.weaknesses,
            "next_action": self.next_action,
            "validation": self.validation,
            "revisit": self.revisit,
        }

