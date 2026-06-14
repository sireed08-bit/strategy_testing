from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from strategy_lab.backtest import PriceBar
from strategy_lab.models import DatasetSpec


def load_price_bars_from_csv(path: Path | str, symbol: str) -> tuple[list[PriceBar], DatasetSpec]:
    source = Path(path)
    bars: list[PriceBar] = []
    with source.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "symbol", "close"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")
        for row in reader:
            if row["symbol"] != symbol:
                continue
            bars.append(
                PriceBar(
                    date=row["date"],
                    symbol=row["symbol"],
                    close=float(row["close"]),
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

