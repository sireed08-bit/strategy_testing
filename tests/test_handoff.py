import json
import zipfile

from strategy_lab.handoff import BatchRequest, create_result_bundle, write_batch_request


def test_write_batch_request_creates_public_safe_request(tmp_path) -> None:
    request = BatchRequest(
        purpose="test request",
        strategy_names=["moving_average_cross"],
        symbols=["SPY"],
        dataset_name="synthetic",
        max_experiments=10,
        shard_count=4,
    )

    path = write_batch_request(request, tmp_path / "batch_request.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["safe_for_public_worker"] is True
    assert payload["strategy_names"] == ["moving_average_cross"]
    assert payload["symbols"] == ["SPY"]


def test_create_result_bundle_includes_manifest_and_inputs(tmp_path) -> None:
    result = tmp_path / "experiment_log.jsonl"
    result.write_text('{"fingerprint":"abc"}\n', encoding="utf-8")

    bundle = create_result_bundle(
        input_paths=[result],
        output_path=tmp_path / "result.bundle.zip",
        manifest={
            "bundle_id": "test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "source": "test",
            "contents": ["experiment_log.jsonl"],
            "sensitivity": "synthetic_public_safe",
        },
    )

    with zipfile.ZipFile(bundle) as archive:
        assert sorted(archive.namelist()) == ["experiment_log.jsonl", "manifest.json"]

