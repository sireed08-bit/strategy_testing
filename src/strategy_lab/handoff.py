from __future__ import annotations

import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class BatchRequest:
    purpose: str
    strategy_names: list[str]
    symbols: list[str]
    dataset_name: str
    max_experiments: int
    shard_count: int
    safe_for_public_worker: bool = True
    request_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)


def write_batch_request(request: BatchRequest, output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request.to_dict(), indent=2), encoding="utf-8")
    return path


def create_result_bundle(
    *,
    input_paths: list[Path],
    output_path: Path,
    manifest: dict,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        for path in input_paths:
            if path.exists():
                bundle.write(path, arcname=path.name)
    return output_path

