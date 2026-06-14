from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass
class ResearchRunRecord:
    purpose: str
    mode: str
    status: str
    experiment_log_path: str
    report_path: str
    experiments_attempted: int
    experiments_created: int
    experiments_skipped_duplicates: int
    strategy_families: dict[str, int]
    grade_counts: dict[str, int]
    next_action: str
    notes: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)


class ResearchRunLedger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, record: ResearchRunRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def latest(self) -> dict | None:
        records = self.records()
        if not records:
            return None
        return records[-1]

