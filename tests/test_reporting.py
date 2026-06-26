from strategy_lab.reporting import build_markdown_report, top_records
from strategy_lab.strategy_ideas import placeholder_records


def test_build_markdown_report_summarizes_records() -> None:
    record = placeholder_records()[0].to_dict()

    report = build_markdown_report([record])

    assert "# Strategy Research Report" in report
    assert "Experiments recorded: 1" in report
    assert "trend_following / moving_average_cross" in report
    assert "Run initial backtest" in report


def test_top_records_deduplicates_same_parameters_different_risk_model() -> None:
    base = {
        "strategy": {
            "name": "rsi_pullback",
            "family": "mean_reversion",
            "parameters": {"rsi_period": 14, "entry_rsi": 30, "exit_rsi": 55},
            "risk_model": {"position_size_pct": 15, "max_hold_days": 5},
        },
        "dataset": {"symbols": ["DIA"]},
        "grade": "promising",
        "score": 71.34,
        "conclusion": "Promising result.",
    }
    duplicate = {**base, "strategy": {**base["strategy"], "risk_model": {"position_size_pct": 20, "max_hold_days": 10}}}
    different = {
        **base,
        "strategy": {**base["strategy"], "parameters": {"rsi_period": 14, "entry_rsi": 35, "exit_rsi": 55}},
        "score": 70.08,
    }

    result = top_records([base, duplicate, different])

    assert len(result) == 2
    assert result[0]["score"] == 71.34
    assert result[1]["score"] == 70.08
