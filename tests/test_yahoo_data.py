from strategy_lab.yahoo_data import rows_from_chart_payload


def _payload() -> dict:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [1104768000, 1104854400, 1104940800],  # 2005-01-03..05
                    "indicators": {
                        "quote": [
                            {
                                "open": [120.0, 119.5, None],  # third bar malformed
                                "high": [121.0, 120.5, 119.0],
                                "low": [119.0, 118.5, 117.0],
                                "close": [120.3, 119.0, None],
                                "volume": [1000000, 900000, 800000],
                            }
                        ],
                        "adjclose": [{"adjclose": [81.17, 80.3, None]}],
                    },
                }
            ]
        }
    }


def test_rows_apply_dividend_split_adjustment_to_ohlc() -> None:
    rows = rows_from_chart_payload("SPY", _payload())
    assert len(rows) == 2  # malformed third bar dropped
    first = rows[0]
    assert first["date"] == "2005-01-03"
    assert first["symbol"] == "SPY"
    assert first["feed"] == "yahoo_adj"
    # Close is the adjusted close; OHLC scaled by the same adj/close ratio so
    # intrabar relationships (e.g. low <= close <= high) survive adjustment.
    assert abs(first["close"] - 81.17) < 1e-9
    ratio = 81.17 / 120.3
    assert abs(first["open"] - round(120.0 * ratio, 4)) < 1e-9
    assert first["low"] <= first["close"] <= first["high"]


def test_append_new_only_preserves_existing_rows_byte_for_byte(tmp_path, monkeypatch) -> None:
    """Yahoo recomputes adjusted history continuously; existing symbols must
    never be silently re-fetched under old fingerprints."""
    import strategy_lab.yahoo_data as ydata

    out = tmp_path / "deep.csv"
    fetched = []

    def _fake_fetch(symbol, start_epoch, end_epoch):
        fetched.append(symbol)
        return _payload()

    monkeypatch.setattr(ydata, "fetch_chart_payload", _fake_fetch)
    ydata.download_deep_history_csv(
        symbols=["SPY"], start="2005-01-01", end="2005-02-01", output_path=out
    )
    original = out.read_text(encoding="utf-8")
    assert fetched == ["SPY"]

    # Second call adds AAPL; SPY must NOT be re-fetched and its rows unchanged.
    ydata.download_deep_history_csv(
        symbols=["SPY", "AAPL"], start="2005-01-01", end="2005-02-01",
        output_path=out, append_new_only=True,
    )
    assert fetched == ["SPY", "AAPL"]
    combined = out.read_text(encoding="utf-8")
    assert combined.startswith(original.rstrip("\n").split("\n")[0] + "\n")  # header
    for line in original.strip().split("\n")[1:]:
        assert line in combined  # every original SPY row survived verbatim


def test_rows_skip_bars_missing_close_or_adjclose() -> None:
    payload = _payload()
    payload["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][1] = None
    rows = rows_from_chart_payload("SPY", payload)
    assert len(rows) == 1  # only the fully-formed first bar survives
