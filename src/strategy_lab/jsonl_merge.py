from __future__ import annotations

import json
from pathlib import Path


def merge_jsonl_files(
    *,
    input_paths: list[Path],
    output_path: Path,
    unique_key: str,
) -> int:
    seen: set[str] = set()
    merged: list[dict] = []
    for path in input_paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                key = str(record[unique_key])
                if key in seen:
                    continue
                seen.add(key)
                merged.append(record)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in merged:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return len(merged)

