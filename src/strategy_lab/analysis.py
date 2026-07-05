"""
Parameter-neighborhood stability analysis.

A trustworthy strategy sits on a *plateau* in parameter space — its neighbours
(combos that differ by a single parameter) also score well. A lonely high score
surrounded by poor neighbours is a fragile spike, almost always an overfit.

This module annotates experiment records with a stability score that blends a
combo's own score with the average score of its one-parameter-away neighbours,
and ranks records so robust plateaus rise above fragile spikes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _combined_params(record: dict) -> dict[str, Any]:
    """Tunable vector = strategy parameters + risk_model (e.g. max_hold_days).
    List values (portfolio symbol sets) become tuples so every value is
    hashable — set/dedup machinery must handle multi-symbol records."""
    strategy = record["strategy"]
    merged = {**strategy.get("parameters", {}), **strategy.get("risk_model", {})}
    return {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in merged.items()
    }


def _symbol(record: dict) -> str:
    return (record["dataset"].get("symbols") or ["?"])[0]


def _sort_key(value: Any) -> tuple[int, Any]:
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def annotate_stability(records: list[dict]) -> list[dict]:
    """Return shallow copies of records with neighbourhood-stability fields added."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        groups[(record["strategy"]["name"], _symbol(record))].append(record)

    annotated: list[dict] = []
    for group in groups.values():
        vectors = [(_combined_params(rec), rec) for rec in group]

        # For each tunable key, index its distinct values in sorted order so we
        # can tell whether two combos are *adjacent* in the grid (one step apart)
        # rather than merely different. Without this, a single-parameter grid
        # would treat every combo as everyone's neighbour and locality is lost.
        all_keys: set[str] = set()
        for params, _ in vectors:
            all_keys |= params.keys()
        value_index: dict[str, dict[Any, int]] = {}
        for key in all_keys:
            ordered = sorted(
                {params[key] for params, _ in vectors if key in params}, key=_sort_key
            )
            value_index[key] = {value: position for position, value in enumerate(ordered)}

        def _is_neighbour(a: dict[str, Any], b: dict[str, Any]) -> bool:
            if a.keys() != b.keys():
                return False
            differing = [key for key in a if a[key] != b[key]]
            if len(differing) != 1:
                return False
            key = differing[0]
            pos_a = value_index[key].get(a[key])
            pos_b = value_index[key].get(b[key])
            return pos_a is not None and pos_b is not None and abs(pos_a - pos_b) == 1

        for params, record in vectors:
            neighbour_scores = [
                other["score"]
                for other_params, other in vectors
                if other is not record and _is_neighbour(params, other_params)
            ]
            enriched = dict(record)
            if neighbour_scores:
                mean_neighbour = sum(neighbour_scores) / len(neighbour_scores)
                enriched["neighbor_count"] = len(neighbour_scores)
                enriched["neighbor_mean_score"] = round(mean_neighbour, 2)
                enriched["neighbor_min_score"] = round(min(neighbour_scores), 2)
                # Blend own score with neighbour support — a spike with weak
                # neighbours is pulled down, a plateau is preserved.
                enriched["stability_score"] = round(
                    0.5 * record["score"] + 0.5 * mean_neighbour, 2
                )
            else:
                enriched["neighbor_count"] = 0
                enriched["neighbor_mean_score"] = None
                enriched["neighbor_min_score"] = None
                enriched["stability_score"] = record["score"]
            annotated.append(enriched)
    return annotated


def cross_symbol_support(records: list[dict]) -> dict[str, int]:
    """
    For each record, count how many OTHER symbols hold the SAME strategy combo
    (identical name + parameters + risk_model) at watch grade or better.

    A real edge on one symbol should at least whisper on its siblings; a combo
    that only ever works on exactly one symbol is usually a fluke of that
    symbol's particular price path. Returns {record fingerprint: support count}.
    """
    combo_symbols: dict[tuple, set[str]] = defaultdict(set)
    for record in records:
        if record.get("grade") in {"watch", "promising", "candidate"}:
            key = (
                record["strategy"]["name"],
                tuple(sorted(_combined_params(record).items())),
            )
            combo_symbols[key].add(_symbol(record))

    support: dict[str, int] = {}
    for record in records:
        key = (
            record["strategy"]["name"],
            tuple(sorted(_combined_params(record).items())),
        )
        others = combo_symbols.get(key, set()) - {_symbol(record)}
        support[record.get("fingerprint", id(record))] = len(others)
    return support


def top_robust_records(records: list[dict], limit: int = 10) -> list[dict]:
    """Rank by stability score (then raw score), deduplicated by tunable vector."""
    annotated = annotate_stability(records)
    ranked = sorted(
        annotated,
        key=lambda rec: (rec["stability_score"], rec["score"]),
        reverse=True,
    )
    seen: set[tuple] = set()
    unique: list[dict] = []
    for record in ranked:
        key = (
            record["strategy"]["name"],
            _symbol(record),
            tuple(sorted(_combined_params(record).items())),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
        if len(unique) >= limit:
            break
    return unique
