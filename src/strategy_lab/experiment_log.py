from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from strategy_lab.models import ExperimentRecord


class DuplicateExperimentError(ValueError):
    pass


class ExperimentLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(
        self,
        record: ExperimentRecord,
        *,
        allow_revisit: bool = False,
        revisit_reason: str = "",
    ) -> None:
        existing = self.find_by_fingerprint(record.fingerprint)
        if existing and not allow_revisit:
            raise DuplicateExperimentError(
                f"Experiment fingerprint already exists: {record.fingerprint}"
            )
        if existing and allow_revisit and not revisit_reason:
            raise DuplicateExperimentError("Revisited experiments need a reason.")

        payload = record.to_dict()
        if allow_revisit:
            payload["revisit"] = {"allowed": True, "reason": revisit_reason}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def fingerprints(self) -> set[str]:
        return {record["fingerprint"] for record in self.records()}

    def find_by_fingerprint(self, fingerprint: str) -> list[dict]:
        return [
            record for record in self.records() if record.get("fingerprint") == fingerprint
        ]

    def append_many(self, records: Iterable[ExperimentRecord]) -> int:
        count = 0
        for record in records:
            self.append(record)
            count += 1
        return count

