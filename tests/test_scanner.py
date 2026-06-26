from strategy_lab.backtest import PriceBar
from strategy_lab.scanner import cross_sectional_scan, load_universe, scan_universe


def _ramp(symbol: str, slope: float, n: int = 80) -> list[PriceBar]:
    return [
        PriceBar(date=f"2021-{(d // 28) % 12 + 1:02d}-{d % 28 + 1:02d}", symbol=symbol, close=100.0 + d * slope)
        for d in range(n)
    ]


def test_cross_sectional_scan_ranks_by_signed_composite() -> None:
    # Three symbols with increasing momentum; roc_20 direction = +1 should rank
    # the steepest ramp first.
    bars_by_symbol = {
        "LOW": _ramp("LOW", 0.1),
        "MID": _ramp("MID", 0.4),
        "HIGH": _ramp("HIGH", 0.9),
    }
    ranked = cross_sectional_scan(bars_by_symbol, {"roc_20": 1.0}, top_n=3)
    symbols_in_order = [row["symbol"] for row in ranked]
    assert symbols_in_order[0] == "HIGH"
    assert symbols_in_order[-1] == "LOW"


def test_cross_sectional_scan_respects_direction_sign() -> None:
    bars_by_symbol = {
        "LOW": _ramp("LOW", 0.1),
        "HIGH": _ramp("HIGH", 0.9),
    }
    # Negative direction flips the ranking.
    ranked = cross_sectional_scan(bars_by_symbol, {"roc_20": -1.0}, top_n=2)
    assert ranked[0]["symbol"] == "LOW"


def test_scan_universe_returns_watchlist_structure() -> None:
    bars_by_symbol = {sym: _ramp(sym, 0.1 + i * 0.2) for i, sym in enumerate(["A", "B", "C", "D"])}
    result = scan_universe(bars_by_symbol, min_abs_ic=0.0, top_n=3)
    assert "watchlist" in result
    assert "indicators_used" in result
    assert len(result["watchlist"]) <= 3


def test_load_universe_reads_config() -> None:
    universe = load_universe()
    assert "SPY" in universe
    assert len(universe) > 20
