from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from strategy_lab.models import ExperimentRecord


class DuplicateExperimentError(ValueError):
    pass


class ExperimentLog:
    """
    Append-only JSONL experiment store with a sidecar fingerprint index.

    Deduplication reads the index file (one fingerprint per line), not the full
    JSONL — so it stays fast even after the log is pruned. The index is the
    durable memory of "every experiment ever run": pruning reject-grade records
    out of the hot log does NOT drop their fingerprints from the index, so pruned
    combos are never silently re-run.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.index_path = self.path.parent / (self.path.stem + ".fingerprints.idx")
        self._fingerprint_cache: set[str] | None = None

    # ── fingerprint index ─────────────────────────────────────────────────────
    def _known_fingerprints(self) -> set[str]:
        if self._fingerprint_cache is None:
            if self.index_path.exists():
                with self.index_path.open("r", encoding="utf-8") as handle:
                    self._fingerprint_cache = {line.strip() for line in handle if line.strip()}
            else:
                # First run on a legacy log: build the index from the records once.
                self._fingerprint_cache = {record["fingerprint"] for record in self.records()}
                self._write_index(self._fingerprint_cache)
        return self._fingerprint_cache

    def _write_index(self, fingerprints: set[str]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with self.index_path.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(fingerprints)))
            if fingerprints:
                handle.write("\n")

    def fingerprints(self) -> set[str]:
        return set(self._known_fingerprints())

    # ── append ────────────────────────────────────────────────────────────────
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
        if not already_seen:
            known.add(record.fingerprint)
            with self.index_path.open("a", encoding="utf-8") as handle:
                handle.write(record.fingerprint + "\n")

    # ── reads ─────────────────────────────────────────────────────────────────
    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

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


def prune_experiment_log(
    log_path: Path | str,
    archive_path: Path | str,
    keep_grades: tuple[str, ...] = ("watch", "promising", "candidate"),
) -> dict:
    """
    Move reject-grade records out of the hot log into an archive, keeping the
    fingerprint index complete so pruned combos are never re-run.

    Returns counts. The archive is append-only (survives repeated prunes) and is
    meant to live in private storage, not the hot working directory.
    """
    log = ExperimentLog(log_path)
    log.fingerprints()  # ensure the index exists and is complete BEFORE we rewrite
    records = log.records()

    keep, archived = [], []
    for record in records:
        (keep if record.get("grade") in keep_grades else archived).append(record)

    archive = Path(archive_path)
    if archived:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as handle:
            for record in archived:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    # Rewrite the hot log with only the kept records. The .idx sidecar is left
    # untouched, so dedup still knows about every archived fingerprint.
    # Write to a temp file and swap atomically: kept records exist ONLY in the
    # hot log (the archive holds rejects), so an in-place truncate-then-write
    # would lose them permanently if the process died mid-rewrite.
    log_file = Path(log_path)
    tmp_file = log_file.with_suffix(log_file.suffix + ".tmp")
    with tmp_file.open("w", encoding="utf-8") as handle:
        for record in keep:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    os.replace(tmp_file, log_file)

    # Track how many records live outside the hot log so callers can report true
    # totals. archived_total accumulates across prunes.
    meta_path = Path(log_path).parent / (Path(log_path).stem + ".meta.json")
    previous = 0
    if meta_path.exists():
        try:
            previous = int(json.loads(meta_path.read_text(encoding="utf-8")).get("archived_total", 0))
        except (ValueError, json.JSONDecodeError):
            previous = 0
    archived_total = previous + len(archived)
    meta_path.write_text(
        json.dumps({"archived_total": archived_total, "archive_path": str(archive)}),
        encoding="utf-8",
    )

    return {
        "kept": len(keep),
        "archived_this_run": len(archived),
        "archived_total": archived_total,
        "hot_log_records": len(keep),
    }


def archived_total(log_path: Path | str) -> int:
    meta_path = Path(log_path).parent / (Path(log_path).stem + ".meta.json")
    if not meta_path.exists():
        return 0
    try:
        return int(json.loads(meta_path.read_text(encoding="utf-8")).get("archived_total", 0))
    except (ValueError, json.JSONDecodeError):
        return 0
