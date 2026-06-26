from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


GRADE_ORDER = {"candidate": 0, "promising": 1, "watch": 2, "reject": 3}


def build_markdown_report(records: list[dict]) -> str:
    created_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Strategy Research Report",
        "",
        f"Generated: {created_at}",
        "",
        "## Summary",
        "",
    ]

    if not records:
        lines.extend(
            [
                "No experiment records are available yet.",
                "",
                "Next action: seed initial strategy ideas, run the first backtests, and save results.",
                "",
            ]
        )
        return "\n".join(lines)

    grade_counts = Counter(record["grade"] for record in records)
    family_counts = Counter(record["strategy"]["family"] for record in records)
    lines.extend(
        [
            f"- Experiments recorded: {len(records)}",
            f"- Candidates: {grade_counts.get('candidate', 0)}",
            f"- Promising: {grade_counts.get('promising', 0)}",
            f"- Watch: {grade_counts.get('watch', 0)}",
            f"- Rejects: {grade_counts.get('reject', 0)}",
            "",
            "## Strategy Families",
            "",
        ]
    )

    for family, count in sorted(family_counts.items()):
        lines.append(f"- {family}: {count}")

    lines.extend(["", "## Best Current Experiments", ""])
    for record in top_records(records):
        lines.append(
            "- "
            f"{record['strategy']['family']} / {record['strategy']['name']}: "
            f"{record['grade']} ({record['score']}) - {record['conclusion']}"
        )

    lines.extend(["", "## Weaknesses Seen", ""])
    weaknesses = Counter(
        weakness
        for record in records
        for weakness in record.get("weaknesses", [])
    )
    if weaknesses:
        for weakness, count in weaknesses.most_common(10):
            lines.append(f"- {weakness}: {count}")
    else:
        lines.append("- No weaknesses have been recorded.")

    lines.extend(["", "## Recommended Next Actions", ""])
    for action in next_actions(records):
        lines.append(f"- {action}")

    lines.append("")
    return "\n".join(lines)


def write_markdown_report(records: list[dict], output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_markdown_report(records), encoding="utf-8")
    return path


def top_records(records: list[dict], limit: int = 5) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for record in sorted(
        records,
        key=lambda record: (GRADE_ORDER.get(record["grade"], 99), -record["score"]),
    ):
        key = (
            record["strategy"]["name"],
            tuple(sorted(record["strategy"]["parameters"].items())),
            tuple(record["dataset"].get("symbols", [])),
        )
        if key not in seen:
            seen.add(key)
            unique.append(record)
        if len(unique) >= limit:
            break
    return unique


def next_actions(records: list[dict]) -> list[str]:
    actions_by_family: dict[str, list[str]] = defaultdict(list)
    for record in records:
        action = record.get("next_action")
        if action:
            actions_by_family[record["strategy"]["family"]].append(action)

    actions: list[str] = []
    for family in sorted(actions_by_family):
        actions.append(f"{family}: {actions_by_family[family][0]}")

    if not actions:
        actions.append("Add next_action notes to experiment records.")

    return actions

