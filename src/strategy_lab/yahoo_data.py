"""
Deep daily history via Yahoo Finance's chart API (no key required).

Why this exists: the Alpaca IEX feed only covers the recent era, so every
conclusion so far rests on 2020-present — one bull regime and one dip. Two
decades of daily bars (2008, 2011, 2015, 2018, 2022...) is the single biggest
robustness lever available: a strategy that only worked in one regime gets
exposed the moment it meets five others.

Prices are FULLY adjusted (splits + dividends): raw OHLC is scaled by each
bar's adjclose/close ratio, so multi-year returns include distributions. This
deliberately differs from the Alpaca dataset (split-only) — it is a separate
dataset with a separate name, so fingerprints never collide and results never
silently mix.

Yahoo's chart API is unofficial. It is used for infrequent bulk pulls of old
history (quarterly at most), not the daily pipeline — if it breaks, nothing in
the autonomous loop breaks with it.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_UA = {"User-Agent": "Mozilla/5.0 (research; strategy-lab)"}


def fetch_chart_payload(symbol: str, start_epoch: int, end_epoch: int) -> dict[str, Any]:
    url = (
        CHART_URL.format(symbol=symbol)
        + f"?period1={start_epoch}&period2={end_epoch}&interval=1d"
    )
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def rows_from_chart_payload(symbol: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse one Yahoo chart payload into our standard CSV row schema."""
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adjcloses = result["indicators"].get("adjclose", [{}])[0].get("adjclose") or []

    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        close = quote["close"][i]
        adj = adjcloses[i] if i < len(adjcloses) else None
        if close is None or adj is None or close <= 0:
            continue  # halted/malformed bar
        ratio = adj / close
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append(
            {
                "date": date,
                "symbol": symbol,
                "open": round(quote["open"][i] * ratio, 4) if quote["open"][i] else None,
                "high": round(quote["high"][i] * ratio, 4) if quote["high"][i] else None,
                "low": round(quote["low"][i] * ratio, 4) if quote["low"][i] else None,
                "close": round(adj, 4),
                "volume": quote["volume"][i],
                "trade_count": None,
                "vwap": None,
                "feed": "yahoo_adj",
            }
        )
    return rows


def download_deep_history_csv(
    *,
    symbols: list[str],
    start: str,
    end: str,
    output_path: Path,
    pause_seconds: float = 1.0,
) -> int:
    """Fetch adjusted daily history for each symbol and write the standard CSV."""
    start_epoch = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    end_epoch = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())

    all_rows: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols):
        payload = fetch_chart_payload(symbol, start_epoch, end_epoch)
        all_rows.extend(rows_from_chart_payload(symbol, payload))
        if index < len(symbols) - 1:
            time.sleep(pause_seconds)  # be a polite guest on an unofficial API

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "date", "symbol", "open", "high", "low", "close",
                "volume", "trade_count", "vwap", "feed",
            ],
        )
        writer.writeheader()
        writer.writerows(all_rows)
    return len(all_rows)
