from strategy_lab.backtest import PriceBar
from strategy_lab.signal_journal import append_signals, evaluate_journal, strategy_key


def _row(bar_date: str, signal: str, symbol: str = "SPY", close: float = 100.0) -> dict:
    return {
        "last_bar_date": bar_date,
        "symbol": symbol,
        "signal": signal,
        "last_close": close,
        "score": 70.0,
        "strategy_identity": {
            "name": "rsi_pullback",
            "parameters": {"entry_rsi": 40, "exit_rsi": 60, "rsi_period": 14},
            "risk_model": {"max_hold_days": 10},
        },
    }


def test_strategy_key_is_stable_and_distinct() -> None:
    a = {"name": "x", "parameters": {"p": 1}, "risk_model": {}}
    b = {"name": "x", "parameters": {"p": 2}, "risk_model": {}}
    assert strategy_key(a) == strategy_key(a)
    assert strategy_key(a) != strategy_key(b)


def test_append_signals_is_idempotent_per_bar_date(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    rows = [_row("2026-06-01", "entry"), _row("2026-06-01", "entry")]  # dup in one call
    assert append_signals(journal, rows) == 1
    # Re-running the same day (e.g. /signals called twice) writes nothing new.
    assert append_signals(journal, [_row("2026-06-01", "entry")]) == 0
    # A new bar date appends.
    assert append_signals(journal, [_row("2026-06-02", "hold_long")]) == 1
    # Error rows are never journalled.
    assert append_signals(journal, [_row("2026-06-03", "error")]) == 0


def test_evaluate_journal_computes_t_plus_1_fills_and_benchmark(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    # Entry signalled on 06-01 (fill at 06-02 close = 102), exit signalled on
    # 06-03 (fill at 06-04 close = 110).
    append_signals(journal, [_row("2026-06-01", "entry")])
    append_signals(journal, [_row("2026-06-02", "hold_long")])
    append_signals(journal, [_row("2026-06-03", "exit")])

    closes = {"2026-06-01": 100.0, "2026-06-02": 102.0, "2026-06-03": 106.0, "2026-06-04": 110.0}
    bars = [PriceBar(date=d, symbol="SPY", close=c) for d, c in sorted(closes.items())]

    report = evaluate_journal(journal, lambda symbol: bars, cost_bps=0.0)
    assert len(report["streams"]) == 1
    stream = report["streams"][0]
    assert stream["closed_trades"] == 1
    trade = stream["trades"][0]
    assert trade["entry_date"] == "2026-06-02"
    assert trade["exit_date"] == "2026-06-04"
    assert abs(trade["net_return_pct"] - round((110.0 / 102.0 - 1) * 100, 2)) < 0.01
    # Benchmark: hold from the first journal bar (06-01 @ 100) to last bar (110).
    assert abs(stream["buy_and_hold_pct"] - 10.0) < 0.01


def test_evaluate_journal_skips_symbols_without_price_data(tmp_path) -> None:
    """External signals may reference symbols no dataset covers — the stream is
    reported as no_price_data instead of blowing up the whole report."""
    journal = tmp_path / "journal.jsonl"
    append_signals(journal, [_row("2026-06-01", "entry", symbol="OBSCURE")])

    def _load(symbol: str):
        raise ValueError(f"No rows found for symbol {symbol}")

    report = evaluate_journal(journal, _load)
    assert len(report["streams"]) == 1
    assert report["streams"][0]["status"] == "no_price_data"


def test_evaluate_journal_marks_open_positions(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    append_signals(journal, [_row("2026-06-01", "entry")])
    closes = {"2026-06-01": 100.0, "2026-06-02": 102.0, "2026-06-03": 104.0}
    bars = [PriceBar(date=d, symbol="SPY", close=c) for d, c in sorted(closes.items())]
    report = evaluate_journal(journal, lambda symbol: bars, cost_bps=0.0)
    stream = report["streams"][0]
    assert stream["closed_trades"] == 0
    assert stream["open_position"] is not None
    assert stream["open_position"]["entry_date"] == "2026-06-02"
    assert abs(stream["open_position"]["unrealized_pct"] - round((104.0 / 102.0 - 1) * 100, 2)) < 0.01
