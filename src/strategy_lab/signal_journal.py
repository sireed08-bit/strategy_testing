"""
Forward signal journal — a walk-forward record no backtest can fake.

Every market day the /signals endpoint computes which top strategies are
signalling entry/exit. Until now those signals were reported and thrown away.
The journal appends them, and as real days accumulate it becomes the only kind
of evidence that is immune to every form of backtest overfitting: signals were
written down BEFORE the future happened.

Evaluation reconstructs each strategy's hypothetical trades from the journal
using the same execution assumptions as the backtest engine (T+1 fills at the
next bar's close, per-side costs) and compares the result to buying and holding
the same symbol over the same span. Months of journal beating its benchmark is
the bar a strategy must clear before anyone should consider paper-trading it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from strategy_lab.backtest import COST_BPS, PriceBar


def strategy_key(strategy: dict[str, Any]) -> str:
    """Stable short identity for a (name, parameters, risk_model) combo."""
    payload = json.dumps(
        {
            "name": strategy.get("name"),
            "parameters": strategy.get("parameters", {}),
            "risk_model": strategy.get("risk_model", {}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _existing_entries(journal_path: Path) -> set[tuple[str, str, str]]:
    if not journal_path.exists():
        return set()
    seen: set[tuple[str, str, str]] = set()
    with journal_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            seen.add((row["bar_date"], row["key"], row["symbol"]))
    return seen


def append_signals(journal_path: Path | str, signal_rows: list[dict[str, Any]]) -> int:
    """
    Append one journal line per (bar_date, strategy, symbol), idempotently.

    Keyed on the BAR date the signal was computed from (not wall-clock), so
    re-running /signals on the same data never double-writes, and weekend or
    holiday re-runs are no-ops.
    """
    path = Path(journal_path)
    seen = _existing_entries(path)
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in signal_rows:
            if row.get("signal") in (None, "error") or not row.get("last_bar_date"):
                continue
            key = strategy_key(row["strategy_identity"])
            dedup = (row["last_bar_date"], key, row["symbol"])
            if dedup in seen:
                continue
            seen.add(dedup)
            handle.write(
                json.dumps(
                    {
                        "bar_date": row["last_bar_date"],
                        "key": key,
                        "symbol": row["symbol"],
                        "strategy": row["strategy_identity"],
                        "signal": row["signal"],
                        "close": row.get("last_close"),
                        "score_at_signal": row.get("score"),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            written += 1
    return written


def evaluate_journal(
    journal_path: Path | str,
    load_bars: Callable[[str], list[PriceBar]],
    cost_bps: float = COST_BPS,
) -> dict[str, Any]:
    """
    Replay the journal into hypothetical forward trades.

    Execution mirrors the backtest engine: an "entry" recorded on bar date d is
    filled at the close of the FIRST bar AFTER d (T+1), an "exit" likewise, and
    each round trip pays cost_bps per side. Positions still open are marked at
    the latest close. Each stream is compared against buying the symbol at its
    own entry fill and holding to the same end point.
    """
    path = Path(journal_path)
    if not path.exists():
        return {"streams": [], "note": "No journal yet — signals accrue on market days."}

    rows = [json.loads(line) for line in path.open("r", encoding="utf-8") if line.strip()]
    rows.sort(key=lambda r: r["bar_date"])

    streams: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        streams.setdefault((row["key"], row["symbol"]), []).append(row)

    cost = cost_bps / 10_000.0
    bars_cache: dict[str, list[PriceBar]] = {}
    reports: list[dict] = []

    for (key, symbol), entries in streams.items():
        if symbol not in bars_cache:
            try:
                bars_cache[symbol] = sorted(load_bars(symbol), key=lambda b: b.date)
            except Exception:
                bars_cache[symbol] = []  # symbol not in any known dataset
        bars = bars_cache[symbol]
        if not bars:
            reports.append({
                "key": key,
                "symbol": symbol,
                "strategy": entries[-1].get("strategy", {}).get("name"),
                "journal_days": len(entries),
                "status": "no_price_data",
            })
            continue
        date_index = {bar.date: i for i, bar in enumerate(bars)}

        def _fill_after(bar_date: str) -> tuple[str, float] | None:
            """Close of the first bar strictly after bar_date (T+1 fill)."""
            idx = date_index.get(bar_date)
            if idx is None or idx + 1 >= len(bars):
                return None  # future bar not known yet — trade still pending
            nxt = bars[idx + 1]
            return nxt.date, nxt.close

        trades: list[dict] = []
        open_entry: tuple[str, float] | None = None
        for row in entries:
            if row["signal"] == "entry" and open_entry is None:
                open_entry = _fill_after(row["bar_date"]) or None
            elif row["signal"] == "exit" and open_entry is not None:
                fill = _fill_after(row["bar_date"])
                if fill is not None:
                    entry_date, entry_price = open_entry
                    exit_date, exit_price = fill
                    net = (exit_price / entry_price) * (1.0 - cost) ** 2 - 1.0
                    trades.append(
                        {
                            "entry_date": entry_date,
                            "exit_date": exit_date,
                            "net_return_pct": round(net * 100.0, 2),
                        }
                    )
                    open_entry = None

        open_position = None
        if open_entry is not None:
            entry_date, entry_price = open_entry
            mark = bars[-1].close
            open_position = {
                "entry_date": entry_date,
                "marked_at": bars[-1].date,
                "unrealized_pct": round((mark / entry_price * (1.0 - cost) - 1.0) * 100.0, 2),
            }

        closed_return = 1.0
        for trade in trades:
            closed_return *= 1.0 + trade["net_return_pct"] / 100.0
        wins = len([t for t in trades if t["net_return_pct"] > 0])

        # Benchmark: hold the symbol from the stream's first journal bar to the
        # latest bar — the honest "should have just bought it" comparison.
        first_idx = date_index.get(entries[0]["bar_date"], 0)
        benchmark_pct = round((bars[-1].close / bars[first_idx].close - 1.0) * 100.0, 2)

        strategy_meta = entries[-1].get("strategy", {})
        reports.append(
            {
                "key": key,
                "symbol": symbol,
                "strategy": strategy_meta.get("name"),
                "parameters": strategy_meta.get("parameters", {}),
                "journal_days": len(entries),
                "first_bar": entries[0]["bar_date"],
                "last_bar": entries[-1]["bar_date"],
                "closed_trades": len(trades),
                "win_rate_pct": round(wins / len(trades) * 100.0, 1) if trades else None,
                "closed_return_pct": round((closed_return - 1.0) * 100.0, 2),
                "open_position": open_position,
                "buy_and_hold_pct": benchmark_pct,
                "excess_vs_hold_pct": round(
                    (closed_return - 1.0) * 100.0 - benchmark_pct, 2
                ),
                "trades": trades[-10:],  # most recent, keep payload bounded
            }
        )

    reports.sort(key=lambda r: r["journal_days"], reverse=True)
    return {"streams": reports, "total_journal_rows": len(rows)}
