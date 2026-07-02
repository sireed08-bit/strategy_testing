import pytest

from strategy_lab.experiment_log import (
    DuplicateExperimentError,
    ExperimentLog,
    archived_total,
    prune_experiment_log,
)
from strategy_lab.strategy_ideas import placeholder_records


def test_experiment_log_rejects_duplicate_without_revisit_reason(tmp_path) -> None:
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    record = placeholder_records()[0]

    log.append(record)

    with pytest.raises(DuplicateExperimentError):
        log.append(record)


def test_experiment_log_allows_documented_revisit(tmp_path) -> None:
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    record = placeholder_records()[0]

    log.append(record)
    log.append(record, allow_revisit=True, revisit_reason="Testing revised data vendor.")

    records = log.records()
    assert len(records) == 2
    assert records[1]["revisit"]["allowed"] is True
    assert records[1]["revisit"]["reason"] == "Testing revised data vendor."


def test_fingerprint_index_persists_and_dedupes(tmp_path) -> None:
    log_path = tmp_path / "experiments.jsonl"
    record = placeholder_records()[0]
    ExperimentLog(log_path).append(record)
    # A fresh instance reads the sidecar index, not the JSONL, and still dedupes.
    assert (tmp_path / "experiments.fingerprints.idx").exists()
    fresh = ExperimentLog(log_path)
    assert record.fingerprint in fresh.fingerprints()
    with pytest.raises(DuplicateExperimentError):
        fresh.append(record)


def test_prune_keeps_index_so_pruned_combos_are_not_rerun(tmp_path) -> None:
    log_path = tmp_path / "experiments.jsonl"
    archive_path = tmp_path / "archive" / "pruned.jsonl"
    log = ExperimentLog(log_path)

    records = placeholder_records()
    # Force a mix of grades: make the first a reject, keep the rest as-is.
    reject = records[0]
    reject.grade = "reject"
    keep = records[1]
    keep.grade = "watch"
    log.append(reject)
    log.append(keep)

    result = prune_experiment_log(log_path, archive_path)
    assert result["archived_this_run"] == 1
    assert result["hot_log_records"] == 1

    # Hot log no longer holds the reject, but the index still knows its fingerprint
    # so re-appending it is still rejected as a duplicate (never re-run).
    pruned = ExperimentLog(log_path)
    assert len(pruned.records()) == 1
    assert reject.fingerprint in pruned.fingerprints()
    with pytest.raises(DuplicateExperimentError):
        pruned.append(reject)
    # Archived record is preserved on disk, and the count is tracked.
    assert archive_path.exists()
    assert archived_total(log_path) == 1
    # The atomic temp-swap leaves no stray .tmp file behind.
    assert not log_path.with_suffix(log_path.suffix + ".tmp").exists()

