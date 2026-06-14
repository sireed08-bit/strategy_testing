from __future__ import annotations

import csv
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALPACA_DATA_BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    api_secret: str


def credentials_from_env() -> AlpacaCredentials:
    api_key = os.environ.get("ALPACA_PAPER_API_KEY") or os.environ.get(
        "APCA_API_KEY_ID"
    )
    api_secret = os.environ.get("ALPACA_PAPER_API_SECRET") or os.environ.get(
        "APCA_API_SECRET_KEY"
    )
    if not api_key or not api_secret:
        raise RuntimeError(
            "Alpaca credentials are missing. Set ALPACA_PAPER_API_KEY and "
            "ALPACA_PAPER_API_SECRET in your local environment or private "
            "GitHub Actions secrets."
        )
    return AlpacaCredentials(api_key=api_key, api_secret=api_secret)


def download_stock_bars_csv(
    *,
    symbols: list[str],
    start: str,
    end: str,
    output_path: Path,
    timeframe: str = "1Day",
    feed: str = "iex",
    credentials: AlpacaCredentials | None = None,
) -> int:
    active_credentials = credentials or credentials_from_env()
    rows = fetch_stock_bars(
        symbols=symbols,
        start=start,
        end=end,
        timeframe=timeframe,
        feed=feed,
        credentials=active_credentials,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "date",
                "symbol",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trade_count",
                "vwap",
                "feed",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def fetch_stock_bars(
    *,
    symbols: list[str],
    start: str,
    end: str,
    timeframe: str,
    feed: str,
    credentials: AlpacaCredentials,
) -> list[dict[str, Any]]:
    if not symbols:
        raise ValueError("At least one symbol is required.")

    rows: list[dict[str, Any]] = []
    next_page_token: str | None = None
    while True:
        payload = request_stock_bars_page(
            symbols=symbols,
            start=start,
            end=end,
            timeframe=timeframe,
            feed=feed,
            credentials=credentials,
            next_page_token=next_page_token,
        )
        for symbol, bars in payload.get("bars", {}).items():
            for bar in bars:
                rows.append(normalize_bar(symbol, bar, feed))
        next_page_token = payload.get("next_page_token")
        if not next_page_token:
            break
    return rows


def request_stock_bars_page(
    *,
    symbols: list[str],
    start: str,
    end: str,
    timeframe: str,
    feed: str,
    credentials: AlpacaCredentials,
    next_page_token: str | None = None,
) -> dict[str, Any]:
    query = {
        "symbols": ",".join(symbols),
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "feed": feed,
        "limit": "10000",
    }
    if next_page_token:
        query["page_token"] = next_page_token
    url = f"{ALPACA_DATA_BASE_URL}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": credentials.api_key,
            "APCA-API-SECRET-KEY": credentials.api_secret,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_bar(symbol: str, bar: dict[str, Any], feed: str) -> dict[str, Any]:
    timestamp = str(bar["t"])
    return {
        "date": timestamp[:10],
        "symbol": symbol,
        "open": bar.get("o"),
        "high": bar.get("h"),
        "low": bar.get("l"),
        "close": bar.get("c"),
        "volume": bar.get("v"),
        "trade_count": bar.get("n"),
        "vwap": bar.get("vw"),
        "feed": feed,
    }

