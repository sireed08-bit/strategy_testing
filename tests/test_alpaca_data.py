from strategy_lab.alpaca_data import normalize_bar


def test_normalize_bar_converts_alpaca_bar_to_csv_row() -> None:
    row = normalize_bar(
        "SPY",
        {
            "t": "2025-01-02T05:00:00Z",
            "o": 100.0,
            "h": 101.0,
            "l": 99.0,
            "c": 100.5,
            "v": 12345,
            "n": 99,
            "vw": 100.2,
        },
        "iex",
    )

    assert row == {
        "date": "2025-01-02",
        "symbol": "SPY",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 12345,
        "trade_count": 99,
        "vwap": 100.2,
        "feed": "iex",
    }

