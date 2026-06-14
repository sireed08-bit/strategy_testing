from strategy_lab.reporting import build_markdown_report
from strategy_lab.strategy_ideas import placeholder_records


def test_build_markdown_report_summarizes_records() -> None:
    record = placeholder_records()[0].to_dict()

    report = build_markdown_report([record])

    assert "# Strategy Research Report" in report
    assert "Experiments recorded: 1" in report
    assert "trend_following / moving_average_cross" in report
    assert "Run initial backtest" in report

