"""
FastAPI server that exposes Strategy Lab to n8n for autonomous scheduled research.

Start from the project root:
    py -3.12 -m uvicorn strategy_lab.server:app --host 127.0.0.1 --port 8078 --reload

n8n reaches this server via http://host.docker.internal:8078/
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from strategy_lab.alpaca_data import credentials_from_env, download_stock_bars_csv

# â”€â”€ load .env automatically so `uvicorn strategy_lab.server:app` just works â”€â”€
def _bootstrap_env() -> None:
    env_file = Path(__file__).resolve().parents[2] / "private_controller" / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import]
        load_dotenv(env_file, override=True, encoding="utf-8-sig")
    except ImportError:
        pass  # python-dotenv not installed; caller must export env vars manually


_bootstrap_env()
from strategy_lab.backtest import build_signals_from_bars
from strategy_lab.batch_runner import run_backtest_batch
from strategy_lab.data_loader import load_price_bars_from_csv
from strategy_lab.experiment_log import ExperimentLog, archived_total, prune_experiment_log
from strategy_lab.models import StrategySpec
from strategy_lab.reporting import top_records
from strategy_lab.run_ledger import ResearchRunLedger

# â”€â”€ project layout â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_ROOT = Path(__file__).resolve().parents[2]

# High-frequency append files (experiment log, index, run ledger, journal, lock)
# must NOT live inside a OneDrive-synced folder: the sync engine racing a file
# that takes thousands of appends per hour corrupted the log mid-file twice
# (2026-06-29 and 2026-07-04, 58 mangled records). STRATEGY_DATA_DIR points the
# hot state at a local, unsynced directory; the repo (code, configs, reports)
# can stay in OneDrive because those files change rarely.
_DATA_ROOT = Path(os.environ.get("STRATEGY_DATA_DIR", "").strip() or (_ROOT / "data"))
_EXPERIMENT_LOG = _DATA_ROOT / "experiments" / "experiment_log.jsonl"
_RUN_LOG = _DATA_ROOT / "runs" / "research_runs.jsonl"
_REPORT = _ROOT / "reports" / "latest.md"
_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA"]
_SCANNER_CSV_NAME = "scanner_universe_bars.csv"
_SIGNAL_JOURNAL = _DATA_ROOT / "signals" / "signal_journal.jsonl"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# â”€â”€ batch write lock â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Only one batch may write the experiment log at a time. Without this, a manual
# /run-all overlapping a scheduled (n8n) one interleaves JSONL lines and corrupts
# the log. The lock is an atomic O_CREAT|O_EXCL file; a stale lock is reclaimed
# so a crash can't deadlock future runs.
#
# HEARTBEAT (added after two incidents): the original design treated "stale" as
# "created more than 2h ago" - but a single deep-history symbol can legitimately
# run for ~2h, so that threshold could not shrink without false-reclaiming live
# batches. 2026-07-06: a batch died silently mid-run (no traceback - likely
# resource exhaustion after ~13h continuous); a second incident had already
# shown that reclaiming a still-live lock loses records (a concurrent /prune
# during the 24h backfill). Fix: batches now touch the lock's mtime once per
# EXPERIMENT (not per symbol - see _touch_lock callers), so a genuinely alive
# batch keeps refreshing every few seconds and the stale threshold can drop to
# 15 minutes: tight enough to recover fast from a real crash, loose enough that
# no live batch is ever mistaken for dead.
import contextlib

_BATCH_LOCK = _DATA_ROOT / "experiments" / ".batch.lock"
_STALE_LOCK_SECONDS = 15 * 60


def _touch_lock() -> None:
    """Best-effort heartbeat: refresh the lock file's mtime. Never raises -
    a heartbeat failure must not abort a research batch."""
    try:
        os.utime(_BATCH_LOCK, None)
    except OSError:
        pass


@contextlib.contextmanager
def _batch_write_lock():
    """Yields a heartbeat callable; callers should invoke it from inside their
    innermost per-experiment loop (see batch_runner.evaluate_and_log_strategies)."""
    _BATCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if _BATCH_LOCK.exists():
        age = datetime.now(timezone.utc).timestamp() - _BATCH_LOCK.stat().st_mtime
        if age < _STALE_LOCK_SECONDS:
            raise HTTPException(
                409,
                "A batch is already running (experiment log is locked). "
                "Wait for it to finish or retry shortly.",
            )
        _BATCH_LOCK.unlink(missing_ok=True)  # stale - reclaim it
    fd = os.open(str(_BATCH_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
        os.close(fd)
        yield _touch_lock
    finally:
        _BATCH_LOCK.unlink(missing_ok=True)


def _notify(title: str, message: str, priority: str = "default") -> None:
    """Fire-and-forget push notification to ntfy.sh. Silently skipped if NTFY_TOPIC is unset."""
    topic = _env("NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode(),
            headers={
                "Title": title,
                "Priority": priority,
                "Content-Type": "text/plain",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # notifications are best-effort; never block the main response


def _private_storage() -> Path:
    root = _env("STRATEGY_PRIVATE_STORAGE_ROOT")
    if not root:
        raise HTTPException(500, "STRATEGY_PRIVATE_STORAGE_ROOT not configured in .env")
    return Path(root)


def _market_data_root() -> Path:
    """Market-data CSVs. Relocatable out of OneDrive via STRATEGY_MARKET_DATA_DIR
    â€” the sync engine that corrupted the append-heavy logs also races the
    weekly whole-file rewrites here, just with a smaller window."""
    override = _env("STRATEGY_MARKET_DATA_DIR")
    if override:
        return Path(override)
    return _private_storage() / "data" / "market_data"


def _data_csv() -> Path:
    p = _market_data_root() / "alpaca_iex_etfs.csv"
    if not p.exists():
        raise HTTPException(500, f"Market data not found at {p}. Call /refresh-data first.")
    return p


def _deep_csv() -> Path:
    p = _market_data_root() / "yahoo_deep_etfs.csv"
    if not p.exists():
        raise HTTPException(500, f"Deep history not found at {p}. Call /refresh-deep-data first.")
    return p


def _select_csv(dataset: str) -> Path:
    if dataset == "deep":
        return _deep_csv()
    if dataset == "scanner":
        p = _scanner_csv()
        if not p.exists():
            raise HTTPException(500, f"Scanner data not found at {p}. Call /scan-refresh first.")
        return p
    if dataset in ("", "default"):
        return _data_csv()
    raise HTTPException(400, f"Unknown dataset '{dataset}'. Use 'default', 'deep' or 'scanner'.")


_VINTAGE_FILE = _DATA_ROOT / "experiments" / "dataset_vintage.json"


def _vintage_end(dataset: str) -> str:
    """Pinned research end-date for a dataset (auto-initialised on first use)."""
    from strategy_lab.data_loader import dataset_vintage_end

    return dataset_vintage_end(_VINTAGE_FILE, dataset or "default", _select_csv(dataset))


def _private_state_repo() -> Path:
    override = _env("STRATEGY_PRIVATE_STATE_REPO")
    return Path(override) if override else _ROOT.parent / "strategy_testing_private_state"


# â”€â”€ app â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app = FastAPI(
    title="Strategy Lab",
    version="0.1.0",
    description=(
        "Autonomous strategy research server. "
        "Driven by n8n for scheduling, Alpaca for data, OpenRouter for suggestions."
    ),
)


# â”€â”€ health â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/health")
def health() -> dict:
    """Pure liveness probe - must never parse the experiment log (that O(n)
    cost is what let the watchdog mistake a busy server for a dead one; see
    docs/handoff/FIX_BRIEF_watchdog_fratricide.md). Everything here is O(1)."""
    storage_root = _env("STRATEGY_PRIVATE_STORAGE_ROOT")
    data_csv_exists = (
        Path(storage_root) / "data" / "market_data" / "alpaca_iex_etfs.csv"
    ).exists() if storage_root else False
    hot_log_bytes = _EXPERIMENT_LOG.stat().st_size if _EXPERIMENT_LOG.exists() else 0
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hot_log_bytes": hot_log_bytes,
        "batch_running": _BATCH_LOCK.exists(),
        "data_csv_exists": data_csv_exists,
        "openrouter_configured": bool(_env("OPENROUTER_API_KEY")),
        "alpaca_configured": bool(_env("ALPACA_PAPER_API_KEY")),
    }


# â”€â”€ status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/status")
def status() -> dict:
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    runs = ResearchRunLedger(_RUN_LOG).records()
    grades = Counter(r.get("grade") for r in records)
    families = Counter(r["strategy"]["family"] for r in records)
    last = runs[-1] if runs else None
    # Reject-grade records may have been pruned to a private archive to keep the
    # hot log fast; count them via the metadata so totals stay honest.
    pruned = archived_total(_EXPERIMENT_LOG)
    return {
        "total_experiments": len(records) + pruned,
        "hot_log_records": len(records),
        "archived_rejects": pruned,
        "candidates": grades.get("candidate", 0),
        "promising": grades.get("promising", 0),
        "watch": grades.get("watch", 0),
        "rejects": grades.get("reject", 0) + pruned,
        "strategy_families": dict(sorted(families.items())),
        "total_runs": len(runs),
        "last_run_at": last.get("created_at") if last else None,
        "last_run_purpose": last.get("purpose") if last else None,
    }


# â”€â”€ report â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/report", response_class=PlainTextResponse)
def report() -> str:
    if not _REPORT.exists():
        raise HTTPException(404, "No report yet. Call /run-all first.")
    return _REPORT.read_text(encoding="utf-8")


# -- weekly report (human digest, pushed via ntfy) ----------------------------

@app.post("/weekly-report")
def weekly_report(days: int = 7) -> dict:
    """Compose a plain-English weekly digest of what the lab found and push it
    to ntfy. Called by the Monday n8n workflow after the research batches; safe
    to call manually any time. One O(n) pass over the hot log - cheap enough
    weekly, and it must NOT be moved into /health (see DEBUGGING.md D11)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    runs = ResearchRunLedger(_RUN_LOG).records()

    week_records = [r for r in records if r.get("created_at", "") >= cutoff]
    week_runs = [r for r in runs if r.get("created_at", "") >= cutoff]
    week_grades = Counter(r.get("grade") for r in week_records)
    new_candidates = [r for r in week_records if r.get("grade") == "candidate"]
    new_promising = [r for r in week_records if r.get("grade") == "promising"]
    all_grades = Counter(r.get("grade") for r in records)

    # Best excess-vs-benchmark seen this week - the one number that would
    # signal genuine alpha if it ever paired with OOS + exam validation.
    best_excess = None
    for r in week_records:
        excess = r.get("metrics", {}).get("excess_return_pct")
        if excess is not None and (best_excess is None or excess > best_excess["excess"]):
            best_excess = {
                "excess": excess,
                "strategy": r["strategy"]["name"],
                "symbol": (r["dataset"]["symbols"] or ["?"])[0],
                "dataset": r["dataset"]["name"],
            }

    lines = [
        f"Week of {datetime.now(timezone.utc).date().isoformat()}",
        "",
        f"Experiments this week: {len(week_records)} across {len(week_runs)} batch runs.",
        f"Week grades: {week_grades.get('candidate', 0)} candidate, "
        f"{week_grades.get('promising', 0)} promising, "
        f"{week_grades.get('watch', 0)} watch, {week_grades.get('reject', 0)} reject.",
        f"All-time: {all_grades.get('candidate', 0)} candidates, "
        f"{all_grades.get('promising', 0)} promising, "
        f"{len(records) + archived_total(_EXPERIMENT_LOG)} total experiments.",
        "",
    ]
    if new_candidates:
        names = ", ".join(
            f"{r['strategy']['name']}/{(r['dataset']['symbols'] or ['?'])[0]}"
            for r in new_candidates[:5]
        )
        lines.append(
            f"CHAMPION-GRADE RESULT: {names}. A validated champion has never "
            "existed - treat with maximum suspicion: check /significance, "
            "/regime-report, and the forward journal before believing it."
        )
    elif new_promising:
        names = ", ".join(
            f"{r['strategy']['name']}/{(r['dataset']['symbols'] or ['?'])[0]}"
            for r in new_promising[:5]
        )
        lines.append(
            f"New promising-grade results this week: {names}. Promising means "
            "'survived the gates so far', not 'beats buy-and-hold' - most "
            "decay under OOS or the final exam."
        )
    else:
        lines.append(
            "No new alpha found this week. That is the expected result: the "
            "21-year verdict stands (no long-only timing alpha on liquid "
            "names). The lab's proven value is DEFENSIVE profiles - see "
            "/defenders and /allocation - plus the forward journal maturing "
            "toward real out-of-sample verdicts."
        )
    if best_excess is not None:
        lines.append(
            f"Best excess vs buy-and-hold this week: "
            f"{best_excess['excess']:+.1f}% ({best_excess['strategy']}/"
            f"{best_excess['symbol']}, {best_excess['dataset']})."
        )
    if not week_runs:
        lines.append(
            "WARNING: zero batch runs in the window - the weekly autonomy "
            "schedule may not be firing. Check n8n and the watchdog log."
        )

    text = "\n".join(lines)
    _notify("Strategy Lab weekly report", text)
    return {
        "report": text,
        "window_days": days,
        "week_experiments": len(week_records),
        "week_runs": len(week_runs),
        "new_candidates": len(new_candidates),
        "new_promising": len(new_promising),
    }


# â”€â”€ top results â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/top-results")
def top_results(limit: int = 10) -> dict:
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    best = top_records(records, limit=limit)
    return {
        "results": [
            {
                "strategy": r["strategy"]["name"],
                "family": r["strategy"]["family"],
                "symbol": (r["dataset"]["symbols"] or ["?"])[0],
                "score": r["score"],
                "grade": r["grade"],
                "parameters": r["strategy"]["parameters"],
                "metrics": {k: round(v, 2) for k, v in r.get("metrics", {}).items()},
            }
            for r in best
        ]
    }


# â”€â”€ robust results (parameter-stability ranked) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/robust-results")
def robust_results(limit: int = 10) -> dict:
    """
    Like /top-results but ranked by parameter-neighbourhood stability instead of
    raw score, so robust plateaus outrank fragile single-combo spikes.
    """
    from strategy_lab.analysis import cross_symbol_support, top_robust_records

    records = ExperimentLog(_EXPERIMENT_LOG).records()
    support = cross_symbol_support(records)
    best = top_robust_records(records, limit=limit)
    return {
        "results": [
            {
                "strategy": r["strategy"]["name"],
                "symbol": (r["dataset"]["symbols"] or ["?"])[0],
                "score": r["score"],
                "stability_score": r.get("stability_score"),
                "neighbor_mean_score": r.get("neighbor_mean_score"),
                "neighbor_count": r.get("neighbor_count"),
                "symbol_support": support.get(r.get("fingerprint"), 0),
                "grade": r["grade"],
                "parameters": r["strategy"]["parameters"],
                "risk_model": r["strategy"].get("risk_model", {}),
                "validation": r.get("validation", {}),
            }
            for r in best
        ]
    }


# â”€â”€ statistical significance of the top results â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/significance")
def significance(limit: int = 8) -> dict:
    """
    Bootstrap test on each top robust strategy's trades: could its average
    trade be zero-edge luck? Trades are re-derived on the same optimisation
    window the record was scored on (final-exam tail excluded). p near 0.5 =
    indistinguishable from noise; and with ~30k experiments run, even p=0.01
    results appear by chance â€” a filter, not a proof.
    """
    from strategy_lab.analysis import top_robust_records
    from strategy_lab.backtest import backtest_trades
    from strategy_lab.batch_runner import trim_final_exam
    from strategy_lab.models import StrategySpec
    from strategy_lab.significance import bootstrap_trade_significance

    records = ExperimentLog(_EXPERIMENT_LOG).records()
    csv = _data_csv()
    end_cap = _vintage_end("default")  # same pinned window the records were scored on
    bars_cache: dict = {}
    results = []
    for record in top_robust_records(records, limit=limit):
        s = record["strategy"]
        symbol = (record["dataset"]["symbols"] or ["?"])[0]
        if symbol not in bars_cache:
            bars, _ = load_price_bars_from_csv(csv, symbol, end_cap=end_cap)
            bars_cache[symbol] = trim_final_exam(bars)
        spec = StrategySpec(
            family=s["family"],
            name=s["name"],
            hypothesis=s.get("hypothesis", ""),
            rules=s.get("rules", {}),
            parameters=s["parameters"],
            risk_model=s.get("risk_model", {}),
        )
        trades = backtest_trades(spec, bars_cache[symbol])
        stats = bootstrap_trade_significance(trades)
        results.append({
            "strategy": s["name"],
            "symbol": symbol,
            "score": record["score"],
            "grade": record["grade"],
            "parameters": s["parameters"],
            **stats,
        })
    return {"results": results, "as_of": datetime.now(timezone.utc).isoformat()}


# â”€â”€ validated champions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/champions")
def champions(limit: int = 10) -> dict:
    """
    The generalisation-based bar a strategy must clear to be called a champion
    (replaces "score >= 80" as the meaningful goal):
      - optimized score >= the promising threshold (currently 65)
      - positive excess return over buy-and-hold
      - final-exam gap <= 10 points with >= 10 exam trades
    A number the optimiser cannot game: it requires performing on data nothing
    in the loop ever touched, and beating doing nothing.
    """
    from strategy_lab.scoring import load_criteria

    promising_floor = float(load_criteria()["grade_thresholds"]["promising"])
    exam = final_exam(limit=limit)
    all_records = ExperimentLog(_EXPERIMENT_LOG).records()
    champions_list = []
    for row in exam["results"]:
        if row.get("error"):
            continue
        excess = None
        # exam rows don't carry optimized metrics; look up excess from the log
        for record in all_records:
            if (
                record["strategy"]["name"] == row["strategy"]
                and (record["dataset"]["symbols"] or ["?"])[0] == row["symbol"]
                and record["strategy"]["parameters"] == row["parameters"]
            ):
                excess = record["metrics"].get("excess_return_pct")
                break
        qualified = (
            row["optimized_score"] >= promising_floor
            and (excess or 0) > 0
            and row["exam_gap"] <= 10
            and row["exam_trade_count"] >= 10
        )
        if qualified:
            champions_list.append({**row, "excess_return_pct": excess})
    if champions_list:
        _notify(
            title=f"Strategy Lab â€” {len(champions_list)} validated champion(s)",
            message="\n".join(
                f"{c['strategy']}/{c['symbol']} opt={c['optimized_score']} exam={c['exam_score']} excess={c['excess_return_pct']:+.1f}%"
                for c in champions_list[:5]
            ),
            priority="high",
        )
    return {
        "criteria": {
            "optimized_score_min": promising_floor,
            "excess_return_positive": True,
            "exam_gap_max": 10,
            "exam_trades_min": 10,
        },
        "champions": champions_list,
        "candidates_reviewed": len(exam["results"]),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# â”€â”€ scanner-universe backtesting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/run-scanner-batch")
def run_scanner_batch(symbols: str = "", limit: int = 400, dataset: str = "scanner") -> dict:
    """
    Run the strategy grid on single names â€” where dispersion and inefficiency
    actually live, unlike the arbitraged-flat index ETFs.

    dataset=deep (preferred for verdicts): 2005+ dividend-adjusted Yahoo
    history â€” single names judged across every regime, not one era.
    dataset=scanner: the 2022+ Alpaca cross-section (fast, recent, split-only).
    """
    default_subset = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,XOM,UNH,XLE,XLF"
    picked = [s.strip().upper() for s in (symbols or default_subset).split(",") if s.strip()]
    csv = _select_csv(dataset)
    end_cap = _vintage_end(dataset)
    date_str = datetime.now(timezone.utc).date().isoformat()
    results = []
    with _batch_write_lock() as heartbeat:
        for symbol in picked:
            try:
                result = run_backtest_batch(
                    experiment_log_path=_EXPERIMENT_LOG,
                    run_log_path=_RUN_LOG,
                    report_path=_REPORT,
                    purpose=f"Scanner-universe batch â€” {symbol} â€” {date_str}",
                    limit=limit,
                    data_csv=csv,
                    symbol=symbol,
                    end_cap=end_cap,
                    heartbeat=heartbeat,
                )
                results.append({
                    "symbol": symbol,
                    "status": "ok",
                    "experiments_created": result.experiments_created,
                })
            except Exception as exc:
                results.append({"symbol": symbol, "status": "error", "error": str(exc)})
    created_total = sum(r.get("experiments_created", 0) for r in results)
    _notify(
        title=f"Strategy Lab â€” scanner-universe batch ({date_str})",
        message=f"{created_total} experiments across {len(picked)} single names.",
        priority="default",
    )
    return {"runs": results, "timestamp": datetime.now(timezone.utc).isoformat()}


# â”€â”€ defenders (crisis-alpha designation + blend analysis) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/defenders")
def defenders(limit: int = 12) -> dict:
    """
    The designation for what this lab verifiably finds: strategies that give up
    bull-market upside to sidestep crashes. Bar: OOS-evaluated (score >= 45),
    max drawdown <= half the benchmark's, positive excess in >= 2/3 of the
    benchmark's down years (min 2 observed). Includes the allocator's question:
    does 50% defender + 50% buy-and-hold beat pure buy-and-hold on Sharpe?
    """
    from strategy_lab.analysis import top_robust_records
    from strategy_lab.batch_runner import trim_final_exam
    from strategy_lab.defenders import evaluate_defender
    from strategy_lab.models import StrategySpec

    stem_to_dataset = {"alpaca_iex_etfs": "default", "yahoo_deep_etfs": "deep",
                       _SCANNER_CSV_NAME.replace(".csv", ""): "scanner"}
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    bars_cache: dict = {}
    results = []
    for record in top_robust_records(records, limit=limit):
        s = record["strategy"]
        if s["name"] in {"regime_switch_pair", "relative_momentum_rotation", "bond_low_risk_off"}:
            continue  # portfolio strategies need their own defender path
        dataset_key = stem_to_dataset.get(record["dataset"].get("name"))
        if dataset_key is None:
            continue
        symbol = (record["dataset"]["symbols"] or ["?"])[0]
        cache_key = (dataset_key, symbol)
        if cache_key not in bars_cache:
            try:
                bars, _ = load_price_bars_from_csv(
                    _select_csv(dataset_key), symbol, end_cap=_vintage_end(dataset_key)
                )
                bars_cache[cache_key] = trim_final_exam(bars)
            except Exception:
                bars_cache[cache_key] = None
        if not bars_cache[cache_key]:
            continue
        spec = StrategySpec(
            family=s["family"], name=s["name"],
            hypothesis=s.get("hypothesis", ""), rules=s.get("rules", {}),
            parameters=s["parameters"], risk_model=s.get("risk_model", {}),
        )
        assessment = evaluate_defender(spec, bars_cache[cache_key], record)
        results.append({
            "strategy": s["name"],
            "symbol": symbol,
            "dataset": dataset_key,
            "score": record["score"],
            "excess_return_pct": record["metrics"].get("excess_return_pct"),
            "parameters": s["parameters"],
            "risk_model": s.get("risk_model", {}),
            **assessment,
        })
    qualified = [r for r in results if r["qualified"]]
    if qualified:
        _notify(
            title=f"Strategy Lab â€” {len(qualified)} validated defender(s)",
            message="\n".join(
                f"{d['strategy']}/{d['symbol']} ({d['dataset']}) dd {d['strategy_dd_pct']}% vs bench {d['benchmark_dd_pct']}%, "
                f"down-years {d['down_years']}, blend-Sharpe-improves={d.get('blend_improves_sharpe')}"
                for d in qualified[:5]
            ),
            priority="default",
        )
    return {
        "defenders": qualified,
        "reviewed": results,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# â”€â”€ allocation (the actionable output: how much defender vs buy-and-hold) â”€â”€â”€â”€â”€

@app.get("/allocation")
def allocation(limit: int = 12) -> dict:
    """
    For each QUALIFIED defender, sweep blend weights (0/25/50/75/100% defender)
    against its own benchmark and report the Sharpe-maximising mix â€” turning
    "defenders exist" into "here is the portfolio an allocator would hold".
    """
    from strategy_lab.analysis import top_robust_records
    from strategy_lab.batch_runner import trim_final_exam
    from strategy_lab.defenders import allocation_sweep, evaluate_defender
    from strategy_lab.models import StrategySpec

    stem_to_dataset = {"alpaca_iex_etfs": "default", "yahoo_deep_etfs": "deep",
                       _SCANNER_CSV_NAME.replace(".csv", ""): "scanner"}
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    bars_cache: dict = {}
    tables = []
    for record in top_robust_records(records, limit=limit):
        s = record["strategy"]
        if s["name"] in {"regime_switch_pair", "relative_momentum_rotation", "bond_low_risk_off"}:
            continue
        dataset_key = stem_to_dataset.get(record["dataset"].get("name"))
        if dataset_key is None:
            continue
        symbol = (record["dataset"]["symbols"] or ["?"])[0]
        cache_key = (dataset_key, symbol)
        if cache_key not in bars_cache:
            try:
                bars, _ = load_price_bars_from_csv(
                    _select_csv(dataset_key), symbol, end_cap=_vintage_end(dataset_key)
                )
                bars_cache[cache_key] = trim_final_exam(bars)
            except Exception:
                bars_cache[cache_key] = None
        if not bars_cache[cache_key]:
            continue
        spec = StrategySpec(
            family=s["family"], name=s["name"],
            hypothesis=s.get("hypothesis", ""), rules=s.get("rules", {}),
            parameters=s["parameters"], risk_model=s.get("risk_model", {}),
        )
        assessment = evaluate_defender(spec, bars_cache[cache_key], record)
        if not assessment["qualified"]:
            continue
        sweep = allocation_sweep(spec, bars_cache[cache_key])
        tables.append({
            "strategy": s["name"],
            "symbol": symbol,
            "dataset": dataset_key,
            "parameters": s["parameters"],
            "risk_model": s.get("risk_model", {}),
            **sweep,
        })
    return {
        "note": (
            "weight_defender=0.0 is pure buy-and-hold of the symbol; 1.0 is the "
            "pure defender sleeve. best_sharpe_weight is the risk-adjusted optimum "
            "on the optimisation window (exam tail excluded)."
        ),
        "allocations": tables,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# â”€â”€ per-year regime breakdown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/regime-report")
def regime_report(limit: int = 5, dataset: str = "default") -> dict:
    """
    Yearly strategy-vs-benchmark returns for the top robust strategies â€”
    "did it earn everything in one lucky year?" Evaluated on the same pinned,
    exam-trimmed window the records were scored on, so the held-out tail's
    years stay unseen.
    """
    from strategy_lab.analysis import top_robust_records
    from strategy_lab.backtest import yearly_breakdown
    from strategy_lab.batch_runner import trim_final_exam
    from strategy_lab.models import StrategySpec

    records = ExperimentLog(_EXPERIMENT_LOG).records()
    csv = _select_csv(dataset)
    end_cap = _vintage_end(dataset)
    dataset_name = csv.stem
    scoped = [r for r in records if r["dataset"].get("name") == dataset_name] or records

    bars_cache: dict = {}
    results = []
    for record in top_robust_records(scoped, limit=limit):
        s = record["strategy"]
        symbol = (record["dataset"]["symbols"] or ["?"])[0]
        if symbol not in bars_cache:
            bars, _ = load_price_bars_from_csv(csv, symbol, end_cap=end_cap)
            bars_cache[symbol] = trim_final_exam(bars)
        spec = StrategySpec(
            family=s["family"],
            name=s["name"],
            hypothesis=s.get("hypothesis", ""),
            rules=s.get("rules", {}),
            parameters=s["parameters"],
            risk_model=s.get("risk_model", {}),
        )
        years = yearly_breakdown(spec, bars_cache[symbol])
        positive_excess_years = len([y for y in years if y["excess_pct"] > 0])
        results.append({
            "strategy": s["name"],
            "symbol": symbol,
            "score": record["score"],
            "grade": record["grade"],
            "parameters": s["parameters"],
            "years": years,
            "positive_excess_years": f"{positive_excess_years}/{len(years)}",
        })
    return {"dataset": dataset, "results": results, "as_of": datetime.now(timezone.utc).isoformat()}


# â”€â”€ final exam (true holdout, never touched by optimisation) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/final-exam")
def final_exam(limit: int = 5) -> dict:
    """
    Evaluate the current top robust strategies on the held-out exam tail â€” the
    most recent FINAL_EXAM_FRACTION of history that scoring, OOS gating, and
    auto-research never see. This is the only honest read on whether a champion
    generalises. Use sparingly: every look at the exam window spends some of its
    statistical independence, so treat it as a periodic audit, not a metric to
    optimise against.
    """
    from strategy_lab.analysis import top_robust_records
    from strategy_lab.batch_runner import FINAL_EXAM_FRACTION, final_exam_start_index
    from strategy_lab.backtest import run_backtest_window
    from strategy_lab.models import StrategySpec
    from strategy_lab.scoring import score_metrics

    records = ExperimentLog(_EXPERIMENT_LOG).records()
    csv = _data_csv()
    end_cap = _vintage_end("default")  # same pinned window the records were scored on
    results = []
    for symbol in _SYMBOLS:
        scoped = [r for r in records if (r["dataset"].get("symbols") or ["?"])[0] == symbol]
        for record in top_robust_records(scoped, limit=limit):
            s = record["strategy"]
            spec = StrategySpec(
                family=s["family"],
                name=s["name"],
                hypothesis=s.get("hypothesis", ""),
                rules=s.get("rules", {}),
                parameters=s["parameters"],
                risk_model=s.get("risk_model", {}),
            )
            bars, _ = load_price_bars_from_csv(csv, symbol, end_cap=end_cap)
            bars = sorted(bars, key=lambda bar: bar.date)
            exam_start = final_exam_start_index(len(bars))
            try:
                exam_metrics = run_backtest_window(spec, bars, exam_start)
                exam_score = score_metrics(exam_metrics).score
                results.append({
                    "strategy": s["name"],
                    "symbol": symbol,
                    "optimized_score": record["score"],
                    "exam_score": exam_score,
                    "exam_gap": round(record["score"] - exam_score, 2),
                    "exam_trade_count": exam_metrics["trade_count"],
                    "exam_return_pct": exam_metrics["annualized_return_pct"],
                    "exam_max_drawdown_pct": exam_metrics["max_drawdown_pct"],
                    "parameters": s["parameters"],
                    "risk_model": s.get("risk_model", {}),
                })
            except ValueError as exc:
                results.append({
                    "strategy": s["name"], "symbol": symbol,
                    "optimized_score": record["score"], "error": str(exc),
                })
    results.sort(key=lambda r: r.get("exam_score", -1.0), reverse=True)
    return {
        "exam_fraction": FINAL_EXAM_FRACTION,
        "note": (
            "Exam tail is excluded from all scoring/OOS/hill-climbing. "
            "Low exam_trade_count means the read is weak, not that the strategy failed."
        ),
        "results": results,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# â”€â”€ live signals â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/signals")
def signals(limit: int = 10) -> dict:
    """
    Apply the top-N strategies to the most recent bars in the historical CSV
    and return whether each is currently signaling entry, hold, or exit.

    Run /refresh-data first each morning to pick up yesterday's close.
    Signal state is based on the last two bars in the CSV â€” no lookahead.
    """
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    best = top_records(records, limit=limit)
    if not best:
        raise HTTPException(404, "No experiments yet. Run /run-all first.")

    csv = _data_csv()
    results = []

    for record in best:
        symbol = (record["dataset"]["symbols"] or ["SPY"])[0]
        s = record["strategy"]
        spec = StrategySpec(
            family=s["family"],
            name=s["name"],
            hypothesis=s.get("hypothesis", ""),
            rules=s.get("rules", {}),
            parameters=s["parameters"],
            risk_model=s.get("risk_model", {}),
        )

        error_detail: str | None = None
        try:
            bars, _ = load_price_bars_from_csv(csv, symbol)
            sig = build_signals_from_bars(spec, bars)

            currently_long = sig[-1] if sig else False
            was_long = sig[-2] if len(sig) >= 2 else False

            if currently_long and not was_long:
                state = "entry"
            elif not currently_long and was_long:
                state = "exit"
            elif currently_long:
                state = "hold_long"
            else:
                state = "flat"

            last_date = bars[-1].date if bars else None
            last_close = bars[-1].close if bars else None

        except Exception as exc:
            # Surface WHY: a swallowed error here makes a broken strategy look
            # permanently quiet instead of visibly failing.
            currently_long = False
            state = "error"
            last_date = None
            last_close = None
            error_detail = f"{type(exc).__name__}: {exc}"

        results.append({
            "strategy": s["name"],
            "family": s["family"],
            "symbol": symbol,
            "score": record["score"],
            "grade": record["grade"],
            "parameters": s["parameters"],
            "strategy_identity": {
                "name": s["name"],
                "parameters": s["parameters"],
                "risk_model": s.get("risk_model", {}),
            },
            "signal": state,
            "currently_long": currently_long,
            "last_bar_date": last_date,
            "last_close": last_close,
            "error": error_detail,
        })

    active = [r for r in results if r["signal"] in ("entry", "exit")]
    if active:
        lines = "\n".join(
            f"{r['signal'].upper()} â€” {r['strategy']} on {r['symbol']} "
            f"(score={r['score']}, close=${r['last_close']})"
            for r in active
        )
        _notify(
            title=f"Strategy Lab â€” {len(active)} signal(s) on {results[0]['last_bar_date']}",
            message=f"{lines}\n\nResearch signals only â€” not live trade recommendations.",
            priority="high",
        )
    errored = [r for r in results if r["signal"] == "error"]

    # Journal today's signals â€” the forward walk-forward record. Best-effort:
    # a journaling failure must never block signal delivery to n8n.
    journaled = 0
    journal_error = None
    try:
        from strategy_lab.signal_journal import append_signals
        journaled = append_signals(_SIGNAL_JOURNAL, results)
    except Exception as exc:
        journal_error = f"{type(exc).__name__}: {exc}"

    return {
        "signals": results,
        "active_signals": active,
        "error_count": len(errored),
        "journaled": journaled,
        "journal_error": journal_error,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "last_bar_date": results[0]["last_bar_date"] if results else None,
    }


# â”€â”€ external signals (TradingView alerts etc. â†’ the same forward journal) â”€â”€â”€â”€â”€

class ExternalSignal(BaseModel):
    source: str = "tradingview"
    symbol: str
    signal: str  # "entry" | "exit" | "hold_long" | "flat"
    strategy_name: str = "unnamed"
    parameters: dict = {}
    price: float | None = None
    bar_date: str = ""  # ISO date; defaults to today (UTC)


@app.post("/external-signal")
def external_signal(sig: ExternalSignal) -> dict:
    """
    Journal a signal from an EXTERNAL system (e.g. a TradingView alert webhook
    routed through n8n) into the same forward journal the lab's own strategies
    use. External ideas earn the identical walk-forward accounting: T+1 fills,
    costs, benchmark comparison â€” so a Pine strategy or a YouTube guru's system
    builds the same kind of forward record before anyone trusts it.
    """
    from strategy_lab.signal_journal import append_signals

    if sig.signal not in {"entry", "exit", "hold_long", "flat"}:
        raise HTTPException(400, f"Unknown signal '{sig.signal}'.")
    bar_date = sig.bar_date or datetime.now(timezone.utc).date().isoformat()
    row = {
        "last_bar_date": bar_date,
        "symbol": sig.symbol.upper(),
        "signal": sig.signal,
        "last_close": sig.price,
        "score": None,
        "strategy_identity": {
            "name": f"external:{sig.source}:{sig.strategy_name}",
            "parameters": sig.parameters,
            "risk_model": {},
        },
    }
    written = append_signals(_SIGNAL_JOURNAL, [row])
    return {
        "journaled": written,
        "deduplicated": written == 0,
        "bar_date": bar_date,
        "stream": row["strategy_identity"]["name"],
    }


# â”€â”€ forward journal (walk-forward record of past signals) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/journal")
def journal() -> dict:
    """
    Forward performance of every signal the lab has journalled: hypothetical
    T+1 fills with costs, per strategy stream, against buy-and-hold of the same
    symbol over the same span. This record was written before the future
    happened â€” the one kind of evidence no backtest can fake.
    """
    from strategy_lab.signal_journal import evaluate_journal

    csv = _data_csv()

    def _load(symbol: str):
        try:
            bars, _ = load_price_bars_from_csv(csv, symbol)
            return bars
        except ValueError:
            # External signals may reference scanner-universe symbols.
            bars, _ = load_price_bars_from_csv(_scanner_csv(), symbol)
            return bars

    report = evaluate_journal(_SIGNAL_JOURNAL, _load)
    report["as_of"] = datetime.now(timezone.utc).isoformat()
    return report


# â”€â”€ journal drift (is live tracking the backtest?) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/journal-drift")
def journal_drift(min_trades: int = 5) -> dict:
    """
    Compare each journalled stream's REALIZED forward results against the
    matching experiment record's backtest expectation (win rate). Large drift
    with enough trades means the live behaviour doesn't match what was tested â€”
    the earliest warning that a backtest was fit to conditions that ended.
    Thin streams are reported but not judged.
    """
    from strategy_lab.signal_journal import evaluate_journal, strategy_key

    csv = _data_csv()

    def _load(symbol: str):
        try:
            bars, _ = load_price_bars_from_csv(csv, symbol)
            return bars
        except ValueError:
            bars, _ = load_price_bars_from_csv(_scanner_csv(), symbol)
            return bars

    journal = evaluate_journal(_SIGNAL_JOURNAL, _load)
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    by_key: dict = {}
    for record in records:
        key = strategy_key(record["strategy"])
        by_key.setdefault((key, (record["dataset"]["symbols"] or ["?"])[0]), record)

    streams = []
    for stream in journal.get("streams", []):
        if stream.get("status") == "no_price_data":
            continue
        expected = by_key.get((stream["key"], stream["symbol"]))
        entry = {
            "strategy": stream.get("strategy"),
            "symbol": stream["symbol"],
            "journal_days": stream["journal_days"],
            "closed_trades": stream["closed_trades"],
            "realized_win_rate_pct": stream.get("win_rate_pct"),
            "expected_win_rate_pct": (expected or {}).get("metrics", {}).get("win_rate_pct"),
            "verdict": "insufficient_trades",
        }
        if stream["closed_trades"] >= min_trades and entry["expected_win_rate_pct"] is not None:
            gap = abs((entry["realized_win_rate_pct"] or 0) - entry["expected_win_rate_pct"])
            entry["win_rate_gap"] = round(gap, 1)
            entry["verdict"] = "drifting" if gap > 25 else "tracking"
        streams.append(entry)
    drifting = [s for s in streams if s["verdict"] == "drifting"]
    if drifting:
        _notify(
            title=f"Strategy Lab â€” {len(drifting)} stream(s) drifting from backtest",
            message="\n".join(f"{d['strategy']}/{d['symbol']} gap={d['win_rate_gap']}pts" for d in drifting[:5]),
            priority="high",
        )
    return {"streams": streams, "as_of": datetime.now(timezone.utc).isoformat()}


# â”€â”€ run single symbol â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class BatchRequest(BaseModel):
    symbol: str = "SPY"
    limit: int = 400
    purpose: str = ""
    dataset: str = "default"


@app.post("/run-batch")
def run_batch(req: BatchRequest) -> dict:
    purpose = req.purpose or (
        f"Autonomous batch â€” {req.symbol} â€” {datetime.now(timezone.utc).date().isoformat()}"
    )
    try:
        with _batch_write_lock() as heartbeat:
            result = run_backtest_batch(
                experiment_log_path=_EXPERIMENT_LOG,
                run_log_path=_RUN_LOG,
                report_path=_REPORT,
                purpose=purpose,
                limit=req.limit,
                data_csv=_select_csv(req.dataset),
                symbol=req.symbol,
                end_cap=_vintage_end(req.dataset),
                heartbeat=heartbeat,
            )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


# â”€â”€ run all 4 symbols (main autonomous action) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/run-all")
def run_all(limit: int = 400, dataset: str = "default") -> dict:
    with _batch_write_lock() as heartbeat:
        return _run_all_locked(limit, dataset, heartbeat)


def _run_all_locked(limit: int, dataset: str = "default", heartbeat=None) -> dict:
    csv = _select_csv(dataset)
    end_cap = _vintage_end(dataset)
    date_str = datetime.now(timezone.utc).date().isoformat()
    results = []
    for symbol in _SYMBOLS:
        purpose = f"Autonomous batch â€” {symbol} â€” {date_str}"
        try:
            result = run_backtest_batch(
                experiment_log_path=_EXPERIMENT_LOG,
                run_log_path=_RUN_LOG,
                report_path=_REPORT,
                purpose=purpose,
                limit=limit,
                data_csv=csv,
                symbol=symbol,
                end_cap=end_cap,
                heartbeat=heartbeat,
            )
            results.append({
                "symbol": symbol,
                "status": "ok",
                "experiments_created": result.experiments_created,
                "experiments_skipped_duplicates": result.experiments_skipped_duplicates,
                "grade_counts": result.grade_counts,
            })
        except Exception as exc:
            results.append({"symbol": symbol, "status": "error", "error": str(exc)})
    payload = {"runs": results, "timestamp": datetime.now(timezone.utc).isoformat()}
    created_total = sum(r.get("experiments_created", 0) for r in results)
    grades = Counter(r.get("grade") for r in ExperimentLog(_EXPERIMENT_LOG).records())
    _notify(
        title=f"Strategy Lab â€” batch complete ({date_str})",
        message=(
            f"{created_total} new experiments across {len(results)} symbols\n"
            f"Candidates: {grades.get('candidate', 0)}  "
            f"Promising: {grades.get('promising', 0)}  "
            f"Watch: {grades.get('watch', 0)}  "
            f"Rejects: {grades.get('reject', 0)}"
        ),
        priority="default",
    )
    return payload


# â”€â”€ maintenance: prune reject-grade records â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/prune")
def prune() -> dict:
    """
    Move reject-grade experiments out of the hot log into a private archive,
    keeping the fingerprint index complete so pruned combos are never re-run.
    Shrinks the working log (~90% is reject-grade) so status/sync/auto-research
    stay fast as the autonomous runs accumulate volume.
    """
    archive_dir = _private_storage() / "archive"
    archive_path = archive_dir / "pruned_rejects.jsonl"
    with _batch_write_lock():
        result = prune_experiment_log(_EXPERIMENT_LOG, archive_path)
    _notify(
        title="Strategy Lab â€” log pruned",
        message=(
            f"Archived {result['archived_this_run']} reject records to private storage. "
            f"Hot log now {result['hot_log_records']} records."
        ),
        priority="default",
    )
    return {**result, "archive_path": str(archive_path), "timestamp": datetime.now(timezone.utc).isoformat()}


# â”€â”€ autonomous research loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/auto-research")
def auto_research(
    top_k: int = 6,
    max_new_per_symbol: int = 60,
    dataset: str = "default",
    objective: str = "score",
) -> dict:
    """
    One bounded hill-climbing round: refine parameters around the current top
    out-of-sample-robust results, backtest the neighbours, keep what survives.
    Safe to schedule â€” it only adds OOS-gated experiments, never edits code/grids.
    dataset=deep refines against the 2005+ multi-regime history.
    objective=defensive ranks seeds by weakest-evidence-minus-drawdown, pointing
    the climb at the crisis-alpha profiles this lab demonstrably finds.
    """
    from strategy_lab.auto_research import run_auto_research

    with _batch_write_lock() as heartbeat:
        result = run_auto_research(
            experiment_log_path=_EXPERIMENT_LOG,
            run_log_path=_RUN_LOG,
            report_path=_REPORT,
            data_csv=_select_csv(dataset),
            symbols=_SYMBOLS,
            top_k=top_k,
            max_new_per_symbol=max_new_per_symbol,
            end_cap=_vintage_end(dataset),
            objective=objective,
            heartbeat=heartbeat,
        )
    headline = (
        f"+{result['experiments_created']} refined experiments. "
        f"Best score {result['best_score_before']} â†’ {result['best_score_after']}"
        + (" (improved)" if result["improved"] else " (no improvement)")
    )
    _notify(
        title="Strategy Lab â€” auto-research round complete",
        message=f"{headline}\nCandidates: {result['candidates']}  Promising: {result['promising']}",
        priority="default",
    )
    return {**result, "headline": headline, "timestamp": datetime.now(timezone.utc).isoformat()}


# â”€â”€ refresh market data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/refresh-data")
def refresh_data(start: str = "2020-01-01", force: bool = False) -> dict:
    output = _market_data_root() / "alpaca_iex_etfs.csv"
    end = datetime.now(timezone.utc).date().isoformat()

    if not force and output.exists():
        age_days = (datetime.now(timezone.utc).timestamp() - output.stat().st_mtime) / 86400
        if age_days < 7:
            return {
                "status": "skipped",
                "reason": f"Data is {age_days:.1f} days old (< 7 days). Pass force=true to override.",
                "path": str(output),
            }

    try:
        creds = credentials_from_env()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))

    try:
        rows = download_stock_bars_csv(
            symbols=_SYMBOLS,
            start=start,
            end=end,
            output_path=output,
            credentials=creds,
        )
        return {"status": "ok", "rows_written": rows, "path": str(output), "end_date": end}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# â”€â”€ dataset vintage control â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/advance-vintage")
def advance_vintage(dataset: str = "default") -> dict:
    """
    Deliberately move a dataset's pinned research end-date to the CSV's current
    last bar. EVERY fingerprint for that dataset rotates â€” the grid re-runs
    under the new vintage on subsequent batches. Do this occasionally and on
    purpose (e.g. quarterly), never as a side effect of a data refresh.
    """
    from strategy_lab.data_loader import advance_dataset_vintage

    result = advance_dataset_vintage(_VINTAGE_FILE, dataset or "default", _select_csv(dataset))
    _notify(
        title="Strategy Lab â€” dataset vintage advanced",
        message=(
            f"{result['dataset']}: {result['previous']} â†’ {result['current']}. "
            "All fingerprints for this dataset rotate; the grid will re-run on "
            "coming batches."
        ),
        priority="default",
    )
    return result


# â”€â”€ multi-symbol (switch/rotation) portfolio research â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_PORTFOLIO_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "TLT", "IEF", "GLD"]
_PORTFOLIO_CSV_NAME = "portfolio_etfs.csv"


def _portfolio_csv() -> Path:
    p = _market_data_root() / _PORTFOLIO_CSV_NAME
    if not p.exists():
        raise HTTPException(500, f"Portfolio data not found at {p}. Call /refresh-portfolio-data first.")
    return p


@app.post("/refresh-portfolio-data")
def refresh_portfolio_data(start: str = "2005-01-01", append: bool = True) -> dict:
    """
    Download the multi-symbol universe (equities + bonds + gold) from Yahoo:
    2005+ WITH dividends. Both properties are essential here â€” the risk-off
    families' entire reason to exist is 2008, and bond returns are mostly
    distributions (price-only TLT understates it by 3-4%/yr, which would
    grossly distort every switch strategy's value). Append-preserving like the
    deep refresh: existing symbols' rows are never silently rewritten.
    """
    from strategy_lab.yahoo_data import download_deep_history_csv

    output = _market_data_root() / _PORTFOLIO_CSV_NAME
    end = datetime.now(timezone.utc).date().isoformat()
    try:
        rows = download_deep_history_csv(
            symbols=_PORTFOLIO_SYMBOLS, start=start, end=end,
            output_path=output, append_new_only=append,
        )
        return {"status": "ok", "rows_total": rows, "path": str(output), "source": "yahoo_adj"}
    except Exception as exc:
        raise HTTPException(502, f"Portfolio data download failed: {exc}")


@app.post("/run-portfolio")
def run_portfolio() -> dict:
    """
    Evaluate every combo in the portfolio_strategies grid: switch/rotation
    strategies holding one symbol at a time (or cash) â€” the classes the
    single-symbol engine cannot express. Same discipline as everything else:
    costs, exam trim, warm OOS, benchmark-relative scoring (benchmark = the
    risk leg's buy-and-hold), fingerprints, dedup, one log.
    """
    from strategy_lab.data_loader import dataset_vintage_end
    from strategy_lab.experiment_generator import expand_grid, load_experiment_space
    from strategy_lab.fingerprints import experiment_fingerprint
    from strategy_lab.models import DatasetSpec, ExperimentRecord, StrategySpec
    from strategy_lab.portfolio_backtest import evaluate_portfolio, portfolio_symbols
    from strategy_lab.scoring import score_metrics

    csv = _portfolio_csv()
    end_cap = dataset_vintage_end(_VINTAGE_FILE, "portfolio", csv)
    space = load_experiment_space()
    items = space.get("portfolio_strategies", [])
    if not items:
        raise HTTPException(400, "No portfolio_strategies section in experiment_space.yaml.")

    with _batch_write_lock() as heartbeat:
        log = ExperimentLog(_EXPERIMENT_LOG)
        known = log.fingerprints()
        bars_cache: dict = {}
        created = skipped = errored = 0
        for item in items:
            for parameters in expand_grid(item.get("parameter_grid", {})):
                for risk_model in expand_grid(item.get("risk_grid", {})):
                    try:
                        heartbeat()
                    except Exception:
                        pass
                    spec = StrategySpec(
                        family=item["family"], name=item["name"],
                        hypothesis=item.get("hypothesis", ""),
                        rules=item.get("rules", {}),
                        parameters=parameters, risk_model=risk_model,
                    )
                    try:
                        needed = portfolio_symbols(spec)
                        for symbol in needed:
                            if symbol not in bars_cache:
                                bars, _ = load_price_bars_from_csv(csv, symbol, end_cap=end_cap)
                                bars_cache[symbol] = bars
                        bars_by_symbol = {s: bars_cache[s] for s in needed}
                        metrics, validation, ds = evaluate_portfolio(spec, bars_by_symbol)
                        dataset = DatasetSpec(
                            name=csv.stem, symbols=ds["symbols"], timeframe="1D",
                            start=ds["start"], end=ds["end"],
                        )
                        fingerprint = experiment_fingerprint(spec, dataset)
                        if fingerprint in known:
                            skipped += 1
                            continue
                        result = score_metrics(metrics)
                        grade = result.grade
                        weaknesses = list(result.weaknesses)
                        if validation.pop("failed_oos", False):
                            grade = "reject"
                            weaknesses.append(f"fails out-of-sample (oos_score={validation.get('oos_score')})")
                        record = ExperimentRecord(
                            strategy=spec, dataset=dataset, metrics=metrics,
                            score=result.score, grade=grade,
                            conclusion=f"Portfolio switch evaluation ({grade}).",
                            fingerprint=fingerprint, weaknesses=weaknesses,
                            next_action="Compare against the risk leg's buy-and-hold.",
                            validation=validation,
                        )
                        log.append(record)
                        known.add(fingerprint)
                        created += 1
                    except (ValueError, KeyError) as exc:
                        errored += 1
    _notify(
        title="Strategy Lab â€” portfolio batch complete",
        message=f"{created} portfolio experiments created ({skipped} known, {errored} errored).",
        priority="default",
    )
    return {
        "created": created, "skipped": skipped, "errored": errored,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# â”€â”€ deep history (Yahoo, 2005+) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/refresh-deep-data")
def refresh_deep_data(start: str = "2005-01-01", symbols: str = "", append: bool = True) -> dict:
    """
    Download two decades of adjusted daily bars from Yahoo's chart API.
    Default mode appends NEW symbols only, preserving existing rows byte-for-
    byte (Yahoo recomputes adjusted history continuously â€” re-downloading an
    existing symbol would silently change data under old fingerprints; do that
    only deliberately with append=false + /advance-vintage).
    """
    from strategy_lab.yahoo_data import download_deep_history_csv

    output = _market_data_root() / "yahoo_deep_etfs.csv"
    end = datetime.now(timezone.utc).date().isoformat()
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or _SYMBOLS
    try:
        rows = download_deep_history_csv(
            symbols=wanted, start=start, end=end, output_path=output,
            append_new_only=append,
        )
        return {
            "status": "ok", "rows_total": rows, "path": str(output),
            "symbols_requested": wanted, "append_new_only": append,
        }
    except Exception as exc:
        raise HTTPException(502, f"Deep history download failed: {exc}")


# â”€â”€ AI research suggestions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/suggest")
def suggest() -> dict:
    api_key = _env("OPENROUTER_API_KEY")
    model = _env("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6")

    if not api_key:
        raise HTTPException(500, "OPENROUTER_API_KEY not set. Add it to private_controller/.env")

    records = ExperimentLog(_EXPERIMENT_LOG).records()
    if not records:
        raise HTTPException(400, "No experiments yet. Call /run-all first.")

    prompt = _build_suggestion_prompt(records)
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
    }).encode()

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/sireed08-bit/strategy_testing",
            "X-Title": "Strategy Lab",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            _notify(
                title="Strategy Lab â€” research suggestions ready",
                message=text[:1500],
                priority="default",
            )
            return {
                "model": model,
                "suggestion": text,
                "experiment_count": len(records),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise HTTPException(502, f"OpenRouter {exc.code}: {detail}")
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _build_suggestion_prompt(records: list[dict]) -> str:
    grades = Counter(r.get("grade") for r in records)
    families = Counter(r["strategy"]["family"] for r in records)
    best = top_records(records, limit=8)
    weaknesses = Counter(w for r in records for w in r.get("weaknesses", []))

    best_lines = "\n".join(
        f"  {i + 1}. {r['strategy']['name']} on {(r['dataset']['symbols'] or ['?'])[0]}: "
        f"score={r['score']}, "
        f"return={r['metrics'].get('annualized_return_pct', 0):.1f}% "
        f"(buy-and-hold {r['metrics'].get('benchmark_return_pct', 0):.1f}%, "
        f"EXCESS {r['metrics'].get('excess_return_pct', 0):+.1f}%), "
        f"drawdown={r['metrics'].get('max_drawdown_pct', 0):.1f}%, "
        f"sharpe={r['metrics'].get('sharpe_ratio', 0):.2f}, "
        f"trades={int(r['metrics'].get('trade_count', 0))}, "
        f"oos={((r.get('validation') or {}).get('oos_score'))}, "
        f"params={r['strategy']['parameters']}"
        for i, r in enumerate(best)
    )
    family_lines = "\n".join(
        f"  - {f}: {c} experiments | grades: "
        + str(dict(Counter(r.get("grade") for r in records if r["strategy"]["family"] == f)))
        for f, c in sorted(families.items())
    )
    weakness_lines = "\n".join(f"  - {w}: {c}" for w, c in weaknesses.most_common(6))

    return f"""You are a quantitative strategy research assistant. Analyze these backtest results and \
provide specific, actionable research suggestions.

## How this lab defines "good" (read carefully â€” it changed)
- Results are NET of 5bps/side costs with T+1 fills and realistic stop fills.
- EXCESS return (strategy minus buy-and-hold on the same bars) carries heavy \
scoring weight: a strategy that loses to simply holding the symbol is NOT a \
finding, whatever its absolute return. Most long-only timing on liquid index \
ETFs fails this bar â€” that is expected, not a bug.
- Every result is out-of-sample gated (train 70% / warm-indicator test 30%), \
and the most recent 15% of history is a held-out final exam nothing optimises \
against. Cross-symbol confirmation and bootstrap significance exist as extra \
filters. Suggest strategies that could survive ALL of that, not just fit.

## Research State ({datetime.now(timezone.utc).date().isoformat()})

Total experiments: {len(records)}
Grades: {dict(grades)}
Scoring: candidate â‰¥ 80, promising 65-79, watch 45-64, reject < 45

## Strategy families
{family_lines}

## Top 8 results (unique parameter sets, deduplicated)
{best_lines}

## Most common weaknesses blocking higher grades
{weakness_lines}

## Task
Provide 4-6 specific, evidence-based research suggestions. For each:
1. **What to test**: exact strategy name, parameter range, or data/rule change
2. **Why**: what the results above suggest about this direction
3. **Expected outcome**: likely grade change or what the experiment would reveal

Prioritise POSITIVE EXCESS RETURN with out-of-sample survival over raw score. \
Be concrete â€” cite specific parameter values, thresholds, or data decisions. \
Do not repeat what has already been exhaustively tested in the results above."""


# â”€â”€ market scanner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _scanner_universe() -> list[str]:
    override = _env("SCANNER_UNIVERSE")
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]
    from strategy_lab.scanner import load_universe
    return load_universe()


def _scanner_csv() -> Path:
    return _market_data_root() / _SCANNER_CSV_NAME


def _load_universe_bars() -> dict:
    """Load OHLCV bars for every universe symbol present in the scanner CSV."""
    csv = _scanner_csv()
    if not csv.exists():
        raise HTTPException(500, f"Scanner data not found at {csv}. Call /scan-refresh first.")
    bars_by_symbol: dict = {}
    for symbol in _scanner_universe():
        try:
            bars, _ = load_price_bars_from_csv(csv, symbol)
            bars_by_symbol[symbol] = bars
        except ValueError:
            continue  # symbol not in CSV (e.g. data fetch failed for it)
    if not bars_by_symbol:
        raise HTTPException(500, "No universe symbols found in scanner CSV.")
    return bars_by_symbol


@app.post("/scan-refresh")
def scan_refresh(start: str = "2022-01-01") -> dict:
    """Download OHLCV bars for the full scanner universe."""
    universe = _scanner_universe()
    output = _scanner_csv()
    end = datetime.now(timezone.utc).date().isoformat()
    try:
        creds = credentials_from_env()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    try:
        rows = download_stock_bars_csv(
            symbols=universe, start=start, end=end, output_path=output, credentials=creds,
        )
        return {"status": "ok", "symbols": len(universe), "rows_written": rows, "path": str(output)}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/evaluate-indicators")
def evaluate_indicators(horizon: int = 5, dataset: str = "scanner") -> dict:
    """
    Rank every indicator by predictive edge (Information Coefficient).
    dataset=scanner: cross-sectional, ~57 symbols, 2022+.
    dataset=deep: the 4 ETFs over 2005+ â€” regime-tested IC, the stronger read
    on whether an indicator's edge is real or an artifact of one market era.
    """
    from strategy_lab.indicator_eval import evaluate_all_indicators

    if dataset == "deep":
        csv = _deep_csv()
        bars_by_symbol = {}
        for symbol in _SYMBOLS:
            bars, _ = load_price_bars_from_csv(csv, symbol)
            bars_by_symbol[symbol] = bars
    else:
        bars_by_symbol = _load_universe_bars()
    results = evaluate_all_indicators(bars_by_symbol, primary_horizon=horizon)
    return {
        "dataset": dataset,
        "universe_size": len(bars_by_symbol),
        "primary_horizon": horizon,
        "indicators": results,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/scan")
def scan(top_n: int = 20, min_abs_ic: float = 0.03, horizon: int = 5) -> dict:
    """Cross-sectionally rank the universe and emit a watchlist."""
    from strategy_lab.scanner import scan_universe

    bars_by_symbol = _load_universe_bars()
    result = scan_universe(
        bars_by_symbol, min_abs_ic=min_abs_ic, top_n=top_n, horizon=horizon
    )
    watchlist = result["watchlist"]
    if watchlist:
        lines = "\n".join(
            f"{i + 1}. {row['symbol']} (composite={row['composite']})"
            for i, row in enumerate(watchlist[:10])
        )
        used = ", ".join(result["indicators_used"].keys())
        _notify(
            title=f"Market Scanner â€” top {min(10, len(watchlist))} of {len(bars_by_symbol)}",
            message=f"Indicators: {used}\n\n{lines}\n\nResearch scan only â€” not trade advice.",
            priority="default",
        )
    return {
        "universe_size": len(bars_by_symbol),
        "watchlist": watchlist,
        "indicators_used": result["indicators_used"],
        "note": result.get("note"),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# â”€â”€ sync private state repo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/sync-private")
def sync_private() -> dict:
    private = _private_state_repo()
    if not private.exists():
        raise HTTPException(
            500,
            f"Private state repo not found at {private}. "
            "Set STRATEGY_PRIVATE_STATE_REPO env var if cloned elsewhere.",
        )

    # The fingerprint index, vintage pins and archived-count meta are part of
    # the backup set: losing the index silently re-runs the entire grid, and
    # losing the vintage file re-triggers fingerprint churn. Since the hot data
    # moved OFF OneDrive (sync-race corruption), this sync is its only backup.
    _idx = _EXPERIMENT_LOG.parent / (_EXPERIMENT_LOG.stem + ".fingerprints.idx")
    _meta = _EXPERIMENT_LOG.parent / (_EXPERIMENT_LOG.stem + ".meta.json")
    file_pairs = [
        (_EXPERIMENT_LOG, private / "data" / "experiments" / "experiment_log.jsonl"),
        (_idx, private / "data" / "experiments" / "experiment_log.fingerprints.idx"),
        (_meta, private / "data" / "experiments" / "experiment_log.meta.json"),
        (_VINTAGE_FILE, private / "data" / "experiments" / "dataset_vintage.json"),
        (_RUN_LOG, private / "data" / "runs" / "research_runs.jsonl"),
        (_SIGNAL_JOURNAL, private / "data" / "signals" / "signal_journal.jsonl"),
        (_REPORT, private / "reports" / "latest.md"),
    ]
    for src, dst in file_pairs:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    try:
        subprocess.run(
            # Stage everything synced under data/ and reports/ â€” the old
            # explicit path list silently omitted newer files (the signal
            # journal was copied but never committed).
            ["git", "-C", str(private), "add", "data", "reports"],
            check=True, capture_output=True,
        )
        no_change = subprocess.run(
            ["git", "-C", str(private), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if no_change.returncode == 0:
            return {"status": "no_changes", "message": "Private state is already up to date."}

        records = ExperimentLog(_EXPERIMENT_LOG).records()
        grades = Counter(r.get("grade") for r in records)
        date_str = datetime.now(timezone.utc).date().isoformat()
        msg = (
            f"autonomous update â€” {date_str}\n\n"
            f"{len(records)} total experiments: "
            f"{grades.get('candidate', 0)} candidate, "
            f"{grades.get('promising', 0)} promising, "
            f"{grades.get('watch', 0)} watch, "
            f"{grades.get('reject', 0)} reject"
        )
        subprocess.run(
            ["git", "-C", str(private), "commit", "-m", msg],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(private), "push", "origin", "main"],
            check=True, capture_output=True,
        )
        return {"status": "ok", "message": f"Synced and pushed â€” {date_str}"}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise HTTPException(500, f"Git error: {stderr}")

