from strategy_lab.scoring import score_metrics


def test_score_metrics_grades_candidate_result() -> None:
    result = score_metrics(
        {
            "annualized_return_pct": 22,
            "max_drawdown_pct": 7,
            "sharpe_ratio": 1.6,
            "sortino_ratio": 2.2,
            "profit_factor": 2.1,
            "trade_count": 220,
            "win_rate_pct": 62,
            "exposure_pct": 35,
            "regime_consistency": 0.9,
            "robustness_score": 0.85,
        }
    )

    assert result.grade == "candidate"
    assert result.score >= 80
    assert result.weaknesses == []


def test_score_metrics_hard_rejects_fragile_result() -> None:
    result = score_metrics(
        {
            "annualized_return_pct": 40,
            "max_drawdown_pct": 42,
            "sharpe_ratio": 1.2,
            "sortino_ratio": 1.5,
            "profit_factor": 0.8,
            "trade_count": 10,
            "win_rate_pct": 55,
            "exposure_pct": 80,
            "regime_consistency": 0.5,
            "robustness_score": 0.4,
        }
    )

    assert result.grade == "reject"
    assert "max_drawdown_pct above 35" in result.weaknesses
    assert "trade_count below 20" in result.weaknesses
    assert "profit_factor below 0.95" in result.weaknesses

