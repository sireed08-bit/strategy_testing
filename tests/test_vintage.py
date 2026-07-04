import csv

from strategy_lab.data_loader import (
    advance_dataset_vintage,
    dataset_vintage_end,
    load_price_bars_from_csv,
)


def _write_csv(path, dates):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "symbol", "close"])
        writer.writeheader()
        for d in dates:
            writer.writerow({"date": d, "symbol": "SPY", "close": "100.0"})


def test_end_cap_slices_bars_and_dataset_end(tmp_path) -> None:
    data = tmp_path / "bars.csv"
    _write_csv(data, ["2026-06-28", "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"])
    bars, dataset = load_price_bars_from_csv(data, "SPY", end_cap="2026-06-30")
    assert len(bars) == 3
    assert dataset.end == "2026-06-30"  # fingerprint-relevant field is pinned
    # Without a cap the fresh bars are visible (live-signal path).
    fresh, fresh_ds = load_price_bars_from_csv(data, "SPY")
    assert len(fresh) == 5
    assert fresh_ds.end == "2026-07-02"


def test_vintage_auto_initialises_and_stays_pinned_across_refresh(tmp_path) -> None:
    data = tmp_path / "bars.csv"
    vintage = tmp_path / "vintage.json"
    _write_csv(data, ["2026-06-30", "2026-07-01"])
    # First use pins to the CSV's current max date.
    assert dataset_vintage_end(vintage, "default", data) == "2026-07-01"
    # A data refresh extends the CSV — the pin must NOT move.
    _write_csv(data, ["2026-06-30", "2026-07-01", "2026-07-08", "2026-07-09"])
    assert dataset_vintage_end(vintage, "default", data) == "2026-07-01"
    # Only a deliberate advance moves it.
    result = advance_dataset_vintage(vintage, "default", data)
    assert result["previous"] == "2026-07-01"
    assert result["current"] == "2026-07-09"
    assert dataset_vintage_end(vintage, "default", data) == "2026-07-09"


def test_vintages_are_independent_per_dataset(tmp_path) -> None:
    default_csv = tmp_path / "default.csv"
    deep_csv = tmp_path / "deep.csv"
    vintage = tmp_path / "vintage.json"
    _write_csv(default_csv, ["2026-07-02"])
    _write_csv(deep_csv, ["2026-07-03"])
    assert dataset_vintage_end(vintage, "default", default_csv) == "2026-07-02"
    assert dataset_vintage_end(vintage, "deep", deep_csv) == "2026-07-03"
    advance_dataset_vintage(vintage, "default", default_csv)
    assert dataset_vintage_end(vintage, "deep", deep_csv) == "2026-07-03"  # untouched
