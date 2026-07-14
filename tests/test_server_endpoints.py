"""
Integration tests for the FastAPI layer — hermetic: all state paths are
monkeypatched to tmp fixtures, no private storage, market data, or network.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import strategy_lab.server as server
from strategy_lab.strategy_ideas import placeholder_records


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_EXPERIMENT_LOG", tmp_path / "experiment_log.jsonl")
    monkeypatch.setattr(server, "_RUN_LOG", tmp_path / "research_runs.jsonl")
    monkeypatch.setattr(server, "_SIGNAL_JOURNAL", tmp_path / "signal_journal.jsonl")
    monkeypatch.setattr(server, "_VINTAGE_FILE", tmp_path / "dataset_vintage.json")
    monkeypatch.setattr(server, "_BATCH_LOCK", tmp_path / ".batch.lock")
    return TestClient(server.app)


def _seed_log(path, n=2, grade="watch"):
    from strategy_lab.experiment_log import ExperimentLog

    log = ExperimentLog(path)
    for record in placeholder_records()[:n]:
        record.grade = grade
        log.append(record)


def test_health_reports_ok_on_empty_log(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["hot_log_bytes"] == 0
    assert body["batch_running"] is False


def test_health_is_ok_even_when_the_log_is_corrupt(client) -> None:
    """/health must be pure liveness - it must never parse the experiment log.
    A corrupt log (or a huge one) must not be able to make the liveness probe
    fail or time out; that coupling is what caused the watchdog to kill a
    healthy-but-busy server twice (see FIX_BRIEF_watchdog_fratricide.md)."""
    server._EXPERIMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    server._EXPERIMENT_LOG.write_text("{not valid json\n", encoding="utf-8")
    health_response = client.get("/health")
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"
    # /status is allowed to be expensive and to fail on a corrupt log - that's
    # the whole point of the split. It is not a liveness probe.
    with pytest.raises(json.JSONDecodeError):
        client.get("/status")


def test_health_batch_running_flips_with_the_lock_file(client) -> None:
    assert client.get("/health").json()["batch_running"] is False
    server._BATCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    server._BATCH_LOCK.write_text("held", encoding="utf-8")
    assert client.get("/health").json()["batch_running"] is True
    server._BATCH_LOCK.unlink()


def test_status_reports_honest_totals_including_archived(client, tmp_path) -> None:
    _seed_log(server._EXPERIMENT_LOG, n=2)
    (tmp_path / "experiment_log.meta.json").write_text(
        json.dumps({"archived_total": 5}), encoding="utf-8"
    )
    body = client.get("/status").json()
    assert body["hot_log_records"] == 2
    assert body["archived_rejects"] == 5
    assert body["total_experiments"] == 7  # hot + archived, never understated


def test_external_signal_journals_and_deduplicates(client) -> None:
    payload = {
        "source": "tradingview",
        "symbol": "nvda",
        "signal": "entry",
        "strategy_name": "pine_test",
        "bar_date": "2026-07-01",
        "price": 120.5,
    }
    first = client.post("/external-signal", json=payload).json()
    assert first["journaled"] == 1
    assert first["stream"] == "external:tradingview:pine_test"
    second = client.post("/external-signal", json=payload).json()
    assert second["journaled"] == 0 and second["deduplicated"] is True
    # Symbol is normalised to upper case in the journal.
    line = json.loads(server._SIGNAL_JOURNAL.read_text(encoding="utf-8").splitlines()[0])
    assert line["symbol"] == "NVDA"


def test_external_signal_rejects_unknown_signal(client) -> None:
    response = client.post(
        "/external-signal",
        json={"symbol": "SPY", "signal": "yolo"},
    )
    assert response.status_code == 400


def test_weekly_report_summarises_the_week(client) -> None:
    _seed_log(server._EXPERIMENT_LOG, n=2, grade="promising")
    body = client.post("/weekly-report").json()
    assert body["week_experiments"] == 2
    assert body["new_promising"] == 2
    assert "promising" in body["report"]
    # Placeholder records carry a fresh created_at, so they land in the window.
    assert "Experiments this week: 2" in body["report"]


def test_weekly_report_on_a_quiet_week_says_so(client) -> None:
    body = client.post("/weekly-report").json()
    assert body["week_experiments"] == 0
    assert body["new_candidates"] == 0
    # The honest default message: no alpha is the expected result.
    assert "No new alpha found this week" in body["report"]
    # And a quiet week with zero runs warns that autonomy may be stalled.
    assert "zero batch runs" in body["report"]


def test_top_results_shape(client) -> None:
    _seed_log(server._EXPERIMENT_LOG, n=2)
    body = client.get("/top-results?limit=5").json()
    assert "results" in body
    for row in body["results"]:
        assert {"strategy", "symbol", "score", "grade", "parameters"} <= set(row)


def test_run_all_rejects_unknown_dataset(client) -> None:
    response = client.post("/run-all?dataset=nonsense")
    assert response.status_code == 400


def test_batch_lock_yields_a_working_heartbeat(client) -> None:
    """The lock context manager must yield a callable that refreshes the lock
    file's mtime — this is what lets the stale-reclaim window stay short
    (15 min) without false-reclaiming a genuinely running batch."""
    import time

    with server._batch_write_lock() as heartbeat:
        assert callable(heartbeat)
        before = server._BATCH_LOCK.stat().st_mtime
        time.sleep(0.05)
        heartbeat()
        after = server._BATCH_LOCK.stat().st_mtime
        assert after >= before
    assert not server._BATCH_LOCK.exists()  # released on exit


def test_heartbeat_never_raises_on_a_missing_lock_file(client) -> None:
    """_touch_lock is called from inside hot research loops — it must be
    silent even if the lock file has vanished underneath it (e.g. a concurrent
    process deleted it), never crashing the batch that called it."""
    assert not server._BATCH_LOCK.exists()
    server._touch_lock()  # must not raise


def test_stale_lock_is_reclaimed_after_the_window(client, monkeypatch) -> None:
    import os

    server._BATCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    server._BATCH_LOCK.write_text("stale", encoding="utf-8")
    old = time.time() - server._STALE_LOCK_SECONDS - 1
    os.utime(server._BATCH_LOCK, (old, old))
    with server._batch_write_lock() as heartbeat:
        assert callable(heartbeat)  # reclaimed, not rejected with 409


def test_fresh_lock_is_not_reclaimed(client) -> None:
    from fastapi import HTTPException

    server._BATCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    server._BATCH_LOCK.write_text("fresh", encoding="utf-8")
    with pytest.raises(HTTPException) as exc_info:
        with server._batch_write_lock():
            pass
    assert exc_info.value.status_code == 409
    server._BATCH_LOCK.unlink()  # cleanup: this path doesn't own the lock
