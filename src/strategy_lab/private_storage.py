from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PRIVATE_STORAGE_DIRS = [
    "data/raw",
    "data/market_data",
    "data/experiment_logs",
    "data/run_ledgers",
    "reports",
    "candidate_reviews",
    "archives",
    "inbox/public_worker_results",
    "outbox/public_worker_requests",
]


@dataclass(frozen=True)
class PrivateStorageLayout:
    root: Path
    directories: list[Path]


def initialize_private_storage(root: Path | str) -> PrivateStorageLayout:
    base = Path(root)
    directories = [base / item for item in PRIVATE_STORAGE_DIRS]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    write_readme(base)
    return PrivateStorageLayout(root=base, directories=directories)


def write_readme(root: Path) -> None:
    readme = root / "README_PRIVATE_STORAGE.md"
    if readme.exists():
        return
    readme.write_text(
        "\n".join(
            [
                "# Strategy Research Private Storage",
                "",
                "This folder is for private research data, run ledgers, reports,",
                "candidate reviews, and public-worker handoff files.",
                "",
                "Do not sync this folder to a public repository.",
                "",
            ]
        ),
        encoding="utf-8",
    )

