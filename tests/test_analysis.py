from strategy_lab.analysis import annotate_stability, top_robust_records


def _record(params: dict, score: float, symbol: str = "SPY", name: str = "rsi_pullback") -> dict:
    return {
        "strategy": {"name": name, "parameters": params, "risk_model": {}},
        "dataset": {"symbols": [symbol]},
        "score": score,
        "grade": "promising" if score >= 65 else "watch",
    }


def test_annotate_stability_blends_neighbor_scores() -> None:
    # A high-scoring combo surrounded by strong neighbours (a plateau) vs an
    # isolated spike surrounded by weak neighbours.
    records = [
        _record({"entry_rsi": 30}, 70.0),  # plateau centre
        _record({"entry_rsi": 32}, 68.0),  # neighbour
        _record({"entry_rsi": 35}, 69.0),  # neighbour (differs in one)
        _record({"entry_rsi": 99}, 80.0),  # lonely spike, neighbours weak
        _record({"entry_rsi": 95}, 40.0),  # weak neighbour of the spike
    ]
    annotated = {r["strategy"]["parameters"]["entry_rsi"]: r for r in annotate_stability(records)}
    plateau = annotated[32]  # middle of the plateau — adjacent to 30 and 35
    spike = annotated[99]
    assert plateau["neighbor_count"] >= 2
    # The spike's raw score is higher, but its stability score is dragged down by
    # the weak neighbour, so the plateau should rank at least comparably.
    assert spike["stability_score"] < spike["score"]


def test_top_robust_records_prefers_supported_plateau_over_spike() -> None:
    records = [
        _record({"entry_rsi": 30}, 70.0),
        _record({"entry_rsi": 32}, 71.0),
        _record({"entry_rsi": 35}, 69.0),
        _record({"entry_rsi": 99}, 82.0),  # isolated spike
        _record({"entry_rsi": 95}, 30.0),
    ]
    top = top_robust_records(records, limit=1)
    assert top[0]["strategy"]["parameters"]["entry_rsi"] in {30, 32, 35}
