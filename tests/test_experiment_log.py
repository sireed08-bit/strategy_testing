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


def test_prune_heartbeats_and_survives_a_broken_heartbeat(tmp_path) -> None:
    """A prune of a large log to a slow (OneDrive-synced) archive path proved
    able to outlive the 15-minute stale-lock window, so prune must heartbeat
    like any other lock holder - and a failing heartbeat must never abort it."""
    log_path = tmp_path / "experiments.jsonl"
    archive_path = tmp_path / "archive" / "pruned.jsonl"
    log = ExperimentLog(log_path)
    records = placeholder_records()[:2]
    for record in records:
        record.grade = "reject"
        log.append(record)

    beats = []
    result = prune_experiment_log(
        log_path, archive_path, heartbeat=lambda: beats.append(1)
    )
    assert result["archived_this_run"] == 2
    assert beats  # fired at least once during the archive write

    # And a raising heartbeat is swallowed, not fatal.
    log2_path = tmp_path / "experiments2.jsonl"
    log2 = ExperimentLog(log2_path)
    rec = placeholder_records()[0]
    rec.grade = "reject"
    log2.append(rec)

    def _broken() -> None:
        raise OSError("simulated disk hiccup")

    result2 = prune_experiment_log(
        log2_path, tmp_path / "archive2" / "pruned.jsonl", heartbeat=_broken
    )
    assert result2["archived_this_run"] == 1


def test_prune_swap_retries_past_a_transient_permission_error(tmp_path, monkeypatch) -> None:
    """On Windows, os.replace is denied while any concurrent reader holds an
    open handle on the hot log - one denied swap aborted a real prune on
    2026-07-13. The swap must retry, not die on the first PermissionError."""
    import os

    import strategy_lab.experiment_log as mod

    log_path = tmp_path / "experiments.jsonl"
    log = ExperimentLog(log_path)
    rec = placeholder_records()[0]
    rec.grade = "reject"
    log.append(rec)

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("target is open by a concurrent reader")
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", _flaky_replace)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)  # no real waiting

    result = prune_experiment_log(log_path, tmp_path / "archive" / "pruned.jsonl")
    assert result["archived_this_run"] == 1
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt
    assert len(ExperimentLog(log_path).records()) == 0  # swap actually landed

