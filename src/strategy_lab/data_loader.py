from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from strategy_lab.backtest import PriceBar
from strategy_lab.models import DatasetSpec


# ── dataset vintages ──────────────────────────────────────────────────────────
# DatasetSpec.end is derived from the last bar, and it is part of every
# experiment fingerprint. Without pinning, any successful data refresh moves
# the end date and silently rotates EVERY fingerprint — the whole grid then
# re-runs as "fresh" and the log fills with mixed data vintages. The vintage
# file pins each dataset's research end-date: batches slice to it, refreshes
# extend the CSV freely underneath (live signals want fresh bars), and the pin
# only moves when advance_dataset_vintage is called deliberately.

def _csv_max_date(csv_path: Path | str) -> str:
    last = ""
    with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["date"] > last:
                last = row["date"]
    if not last:
        raise ValueError(f"No rows in {csv_path}")
    return last


def dataset_vintage_end(vintage_path: Path | str, dataset_name: str, csv_path: Path | str) -> str:
    """
    The pinned research end-date for a dataset. Auto-initialises to the CSV's
    current max date on first use — so pinning never orphans results that were
    computed just before it was introduced.
    """
    path = Path(vintage_path)
    vintages: dict[str, str] = {}
    if path.exists():
        vintages = json.loads(path.read_text(encoding="utf-8"))
    if dataset_name not in vintages:
        vintages[dataset_name] = _csv_max_date(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(vintages, sort_keys=True, indent=2), encoding="utf-8")
    return vintages[dataset_name]


def advance_dataset_vintage(vintage_path: Path | str, dataset_name: str, csv_path: Path | str) -> dict:
    """
    Deliberately move a dataset's pin to the CSV's current max date. Every
    fingerprint for that dataset rotates — the grid re-runs under the new
    vintage. This is a conscious, occasional decision (e.g. quarterly), not a
    side effect of a data refresh.
    """
    path = Path(vintage_path)
    vintages: dict[str, str] = {}
    if path.exists():
        vintages = json.loads(path.read_text(encoding="utf-8"))
    previous = vintages.get(dataset_name)
    vintages[dataset_name] = _csv_max_date(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vintages, sort_keys=True, indent=2), encoding="utf-8")
    return {"dataset": dataset_name, "previous": previous, "current": vintages[dataset_name]}


def _opt_float(value: str | None) -> float | None:
    """Parse an optional numeric CSV cell, tolerating blanks and bad values."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_price_bars_from_csv(
    path: Path | str,
    symbol: str,
    end_cap: str | None = None,
) -> tuple[list[PriceBar], DatasetSpec]:
    """
    Load bars for one symbol; when end_cap (ISO date) is given, only bars up to
    and including that date are returned and the DatasetSpec reflects the
    capped range. Research batches pass the pinned vintage date here so a data
    refresh cannot silently rotate every experiment fingerprint; live-signal
    paths pass None and always see the freshest bars.
    """
    source = Path(path)
    bars: list[PriceBar] = []
    with source.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "symbol", "close"}
        present = set(reader.fieldnames or [])
        missing = required - present
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")
        # Optional OHLCV columns are read when present and left as None otherwise.
        for row in reader:
            if row["symbol"] != symbol:
                continue
            if end_cap is not None and row["date"] > end_cap:
                continue
            bars.append(
                PriceBar(
                    date=row["date"],
                    symbol=row["symbol"],
                    close=float(row["close"]),
                    open=_opt_float(row.get("open")),
                    high=_opt_float(row.get("high")),
                    low=_opt_float(row.get("low")),
                    volume=_opt_float(row.get("volume")),
                    vwap=_opt_float(row.get("vwap")),
                )
            )

    if not bars:
        raise ValueError(f"No rows found for symbol {symbol} in {source}")

    ordered = sorted(bars, key=lambda bar: bar.date)
    return ordered, DatasetSpec(
        name=source.stem,
        symbols=[symbol],
        timeframe="1D",
        start=ordered[0].date,
        end=ordered[-1].date,
    )


def synthetic_price_bars(
    *,
    symbol: str = "SPY",
    days: int = 756,
    start: date = date(2020, 1, 1),
) -> tuple[list[PriceBar], DatasetSpec]:
    bars: list[PriceBar] = []
    price = 100.0
    for index in range(days):
        cycle = ((index % 60) - 30) / 30
        drift = 0.00045
        shock = cycle * 0.004
        price = max(1.0, price * (1.0 + drift + shock))
        bars.append(
            PriceBar(
                date=(start + timedelta(days=index)).isoformat(),
                symbol=symbol,
                close=round(price, 4),
            )
        )

    return bars, DatasetSpec(
        name=f"synthetic_{symbol.lower()}_{days}d",
        symbols=[symbol],
        timeframe="1D",
        start=bars[0].date,
        end=bars[-1].date,
    )

