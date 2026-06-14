import pytest

from strategy_lab.experiment_log import DuplicateExperimentError, ExperimentLog
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

