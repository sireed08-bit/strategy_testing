import json

from strategy_lab.jsonl_merge import merge_jsonl_files


def test_merge_jsonl_files_deduplicates_by_key(tmp_path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    first.write_text(
        json.dumps({"fingerprint": "a", "value": 1}) + "\n"
        + json.dumps({"fingerprint": "b", "value": 2}) + "\n",
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"fingerprint": "b", "value": 3}) + "\n"
        + json.dumps({"fingerprint": "c", "value": 4}) + "\n",
        encoding="utf-8",
    )

    merged = merge_jsonl_files(
        input_paths=[first, second],
        output_path=output,
        unique_key="fingerprint",
    )

    lines = output.read_text(encoding="utf-8").splitlines()
    assert merged == 3
    assert len(lines) == 3
