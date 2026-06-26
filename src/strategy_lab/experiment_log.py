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
        # Lazily-loaded set of known fingerprints. Caching this avoids re-reading
        # and re-parsing the entire JSONL on every append — which made batch
        # inserts O(n²) and brought large rebuilds to a crawl. Loaded once from
        # disk on first use, then kept in sync as records are appended.
        self._fingerprint_cache: set[str] | None = None

    def _known_fingerprints(self) -> set[str]:
        if self._fingerprint_cache is None:
            self._fingerprint_cache = {record["fingerprint"] for record in self.records()}
        return self._fingerprint_cache

    def append(
        self,
        record: ExperimentRecord,
        *,
        allow_revisit: bool = False,
        revisit_reason: str = "",
    ) -> None:
        known = self._known_fingerprints()
        already_seen = record.fingerprint in known
        if already_seen and not allow_revisit:
            raise DuplicateExperimentError(
                f"Experiment fingerprint already exists: {record.fingerprint}"
            )
        if already_seen and allow_revisit and not revisit_reason:
            raise DuplicateExperimentError("Revisited experiments need a reason.")

        payload = record.to_dict()
        if allow_revisit:
            payload["revisit"] = {"allowed": True, "reason": revisit_reason}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        known.add(record.fingerprint)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def fingerprints(self) -> set[str]:
        return set(self._known_fingerprints())

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

