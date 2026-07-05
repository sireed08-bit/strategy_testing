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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from strategy_lab.alpaca_data import credentials_from_env, download_stock_bars_csv

# ── load .env automatically so `uvicorn strategy_lab.server:app` just works ──
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

# ── project layout ────────────────────────────────────────────────────────────
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


# ── batch write lock ──────────────────────────────────────────────────────────
# Only one batch may write the experiment log at a time. Without this, a manual
# /run-all overlapping a scheduled (n8n) one interleaves JSONL lines and corrupts
# the log. The lock is an atomic O_CREAT|O_EXCL file; a stale lock (dead process
# or >2h old) is reclaimed so a crash can't deadlock future runs.
import contextlib

_BATCH_LOCK = _DATA_ROOT / "experiments" / ".batch.lock"
_STALE_LOCK_SECONDS = 2 * 60 * 60


@contextlib.contextmanager
def _batch_write_lock():
    _BATCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if _BATCH_LOCK.exists():
        age = datetime.now(timezone.utc).timestamp() - _BATCH_LOCK.stat().st_mtime
        if age < _STALE_LOCK_SECONDS:
            raise HTTPException(
                409,
                "A batch is already running (experiment log is locked). "
                "Wait for it to finish or retry shortly.",
            )
        _BATCH_LOCK.unlink(missing_ok=True)  # stale — reclaim it
    fd = os.open(str(_BATCH_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
        os.close(fd)
        yield
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


def _data_csv() -> Path:
    p = _private_storage() / "data" / "market_data" / "alpaca_iex_etfs.csv"
    if not p.exists():
        raise HTTPException(500, f"Market data not found at {p}. Call /refresh-data first.")
    return p


def _deep_csv() -> Path:
    p = _private_storage() / "data" / "market_data" / "yahoo_deep_etfs.csv"
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


# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Strategy Lab",
    version="0.1.0",
    description=(
        "Autonomous strategy research server. "
        "Driven by n8n for scheduling, Alpaca for data, OpenRouter for suggestions."
    ),
)


# ── health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    storage_root = _env("STRATEGY_PRIVATE_STORAGE_ROOT")
    data_csv_exists = (
        Path(storage_root) / "data" / "market_data" / "alpaca_iex_etfs.csv"
    ).exists() if storage_root else False
    records = ExperimentLog(_EXPERIMENT_LOG).records()
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_experiments": len(records),
        "data_csv_exists": data_csv_exists,
        "openrouter_configured": bool(_env("OPENROUTER_API_KEY")),
        "alpaca_configured": bool(_env("ALPACA_PAPER_API_KEY")),
    }


# ── status ────────────────────────────────────────────────────────────────────

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


# ── report ────────────────────────────────────────────────────────────────────

@app.get("/report", response_class=PlainTextResponse)
def report() -> str:
    if not _REPORT.exists():
        raise HTTPException(404, "No report yet. Call /run-all first.")
    return _REPORT.read_text(encoding="utf-8")


# ── top results ───────────────────────────────────────────────────────────────

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


# ── robust results (parameter-stability ranked) ───────────────────────────────

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


# ── statistical significance of the top results ───────────────────────────────

@app.get("/significance")
def significance(limit: int = 8) -> dict:
    """
    Bootstrap test on each top robust strategy's trades: could its average
    trade be zero-edge luck? Trades are re-derived on the same optimisation
    window the record was scored on (final-exam tail excluded). p near 0.5 =
    indistinguishable from noise; and with ~30k experiments run, even p=0.01
    results appear by chance — a filter, not a proof.
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


# ── validated champions ───────────────────────────────────────────────────────

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
            title=f"Strategy Lab — {len(champions_list)} validated champion(s)",
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


# ── scanner-universe backtesting ──────────────────────────────────────────────

@app.post("/run-scanner-batch")
def run_scanner_batch(symbols: str = "", limit: int = 400) -> dict:
    """
    Run the strategy grid on single names from the scanner universe — where
    dispersion and inefficiency actually live, unlike the arbitraged-flat index
    ETFs. Default subset: liquid mega-caps + two sector ETFs. Scanner history
    starts 2022, so expect thinner OOS windows; the same validation machinery
    applies unchanged.
    """
    default_subset = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,XOM,UNH,XLE,XLF"
    picked = [s.strip().upper() for s in (symbols or default_subset).split(",") if s.strip()]
    csv = _select_csv("scanner")
    end_cap = _vintage_end("scanner")
    date_str = datetime.now(timezone.utc).date().isoformat()
    results = []
    with _batch_write_lock():
        for symbol in picked:
            try:
                result = run_backtest_batch(
                    experiment_log_path=_EXPERIMENT_LOG,
                    run_log_path=_RUN_LOG,
                    report_path=_REPORT,
                    purpose=f"Scanner-universe batch — {symbol} — {date_str}",
                    limit=limit,
                    data_csv=csv,
                    symbol=symbol,
                    end_cap=end_cap,
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
        title=f"Strategy Lab — scanner-universe batch ({date_str})",
        message=f"{created_total} experiments across {len(picked)} single names.",
        priority="default",
    )
    return {"runs": results, "timestamp": datetime.now(timezone.utc).isoformat()}


# ── per-year regime breakdown ─────────────────────────────────────────────────

@app.get("/regime-report")
def regime_report(limit: int = 5, dataset: str = "default") -> dict:
    """
    Yearly strategy-vs-benchmark returns for the top robust strategies —
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


# ── final exam (true holdout, never touched by optimisation) ──────────────────

@app.get("/final-exam")
def final_exam(limit: int = 5) -> dict:
    """
    Evaluate the current top robust strategies on the held-out exam tail — the
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


# ── live signals ─────────────────────────────────────────────────────────────

@app.get("/signals")
def signals(limit: int = 10) -> dict:
    """
    Apply the top-N strategies to the most recent bars in the historical CSV
    and return whether each is currently signaling entry, hold, or exit.

    Run /refresh-data first each morning to pick up yesterday's close.
    Signal state is based on the last two bars in the CSV — no lookahead.
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
            f"{r['signal'].upper()} — {r['strategy']} on {r['symbol']} "
            f"(score={r['score']}, close=${r['last_close']})"
            for r in active
        )
        _notify(
            title=f"Strategy Lab — {len(active)} signal(s) on {results[0]['last_bar_date']}",
            message=f"{lines}\n\nResearch signals only — not live trade recommendations.",
            priority="high",
        )
    errored = [r for r in results if r["signal"] == "error"]

    # Journal today's signals — the forward walk-forward record. Best-effort:
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


# ── forward journal (walk-forward record of past signals) ────────────────────

@app.get("/journal")
def journal() -> dict:
    """
    Forward performance of every signal the lab has journalled: hypothetical
    T+1 fills with costs, per strategy stream, against buy-and-hold of the same
    symbol over the same span. This record was written before the future
    happened — the one kind of evidence no backtest can fake.
    """
    from strategy_lab.signal_journal import evaluate_journal

    csv = _data_csv()

    def _load(symbol: str):
        bars, _ = load_price_bars_from_csv(csv, symbol)
        return bars

    report = evaluate_journal(_SIGNAL_JOURNAL, _load)
    report["as_of"] = datetime.now(timezone.utc).isoformat()
    return report


# ── run single symbol ─────────────────────────────────────────────────────────

class BatchRequest(BaseModel):
    symbol: str = "SPY"
    limit: int = 400
    purpose: str = ""
    dataset: str = "default"


@app.post("/run-batch")
def run_batch(req: BatchRequest) -> dict:
    purpose = req.purpose or (
        f"Autonomous batch — {req.symbol} — {datetime.now(timezone.utc).date().isoformat()}"
    )
    try:
        with _batch_write_lock():
            result = run_backtest_batch(
                experiment_log_path=_EXPERIMENT_LOG,
                run_log_path=_RUN_LOG,
                report_path=_REPORT,
                purpose=purpose,
                limit=req.limit,
                data_csv=_select_csv(req.dataset),
                symbol=req.symbol,
                end_cap=_vintage_end(req.dataset),
            )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── run all 4 symbols (main autonomous action) ────────────────────────────────

@app.post("/run-all")
def run_all(limit: int = 400, dataset: str = "default") -> dict:
    with _batch_write_lock():
        return _run_all_locked(limit, dataset)


def _run_all_locked(limit: int, dataset: str = "default") -> dict:
    csv = _select_csv(dataset)
    end_cap = _vintage_end(dataset)
    date_str = datetime.now(timezone.utc).date().isoformat()
    results = []
    for symbol in _SYMBOLS:
        purpose = f"Autonomous batch — {symbol} — {date_str}"
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
        title=f"Strategy Lab — batch complete ({date_str})",
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


# ── maintenance: prune reject-grade records ───────────────────────────────────

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
        title="Strategy Lab — log pruned",
        message=(
            f"Archived {result['archived_this_run']} reject records to private storage. "
            f"Hot log now {result['hot_log_records']} records."
        ),
        priority="default",
    )
    return {**result, "archive_path": str(archive_path), "timestamp": datetime.now(timezone.utc).isoformat()}


# ── autonomous research loop ──────────────────────────────────────────────────

@app.post("/auto-research")
def auto_research(top_k: int = 6, max_new_per_symbol: int = 60) -> dict:
    """
    One bounded hill-climbing round: refine parameters around the current top
    out-of-sample-robust results, backtest the neighbours, keep what survives.
    Safe to schedule — it only adds OOS-gated experiments, never edits code/grids.
    """
    from strategy_lab.auto_research import run_auto_research

    with _batch_write_lock():
        result = run_auto_research(
            experiment_log_path=_EXPERIMENT_LOG,
            run_log_path=_RUN_LOG,
            report_path=_REPORT,
            data_csv=_data_csv(),
            symbols=_SYMBOLS,
            top_k=top_k,
            max_new_per_symbol=max_new_per_symbol,
            end_cap=_vintage_end("default"),
        )
    headline = (
        f"+{result['experiments_created']} refined experiments. "
        f"Best score {result['best_score_before']} → {result['best_score_after']}"
        + (" (improved)" if result["improved"] else " (no improvement)")
    )
    _notify(
        title="Strategy Lab — auto-research round complete",
        message=f"{headline}\nCandidates: {result['candidates']}  Promising: {result['promising']}",
        priority="default",
    )
    return {**result, "headline": headline, "timestamp": datetime.now(timezone.utc).isoformat()}


# ── refresh market data ───────────────────────────────────────────────────────

@app.post("/refresh-data")
def refresh_data(start: str = "2020-01-01", force: bool = False) -> dict:
    output = _private_storage() / "data" / "market_data" / "alpaca_iex_etfs.csv"
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


# ── dataset vintage control ───────────────────────────────────────────────────

@app.post("/advance-vintage")
def advance_vintage(dataset: str = "default") -> dict:
    """
    Deliberately move a dataset's pinned research end-date to the CSV's current
    last bar. EVERY fingerprint for that dataset rotates — the grid re-runs
    under the new vintage on subsequent batches. Do this occasionally and on
    purpose (e.g. quarterly), never as a side effect of a data refresh.
    """
    from strategy_lab.data_loader import advance_dataset_vintage

    result = advance_dataset_vintage(_VINTAGE_FILE, dataset or "default", _select_csv(dataset))
    _notify(
        title="Strategy Lab — dataset vintage advanced",
        message=(
            f"{result['dataset']}: {result['previous']} → {result['current']}. "
            "All fingerprints for this dataset rotate; the grid will re-run on "
            "coming batches."
        ),
        priority="default",
    )
    return result


# ── deep history (Yahoo, 2005+) ───────────────────────────────────────────────

@app.post("/refresh-deep-data")
def refresh_deep_data(start: str = "2005-01-01") -> dict:
    """
    Download two decades of adjusted daily bars for the backtest ETFs from
    Yahoo's chart API. Infrequent bulk pull (quarterly at most) — the daily
    Alpaca pipeline is unaffected if Yahoo ever breaks. Run deep batches with
    /run-all?dataset=deep — a separate dataset with separate fingerprints.
    """
    from strategy_lab.yahoo_data import download_deep_history_csv

    output = _private_storage() / "data" / "market_data" / "yahoo_deep_etfs.csv"
    end = datetime.now(timezone.utc).date().isoformat()
    try:
        rows = download_deep_history_csv(
            symbols=_SYMBOLS, start=start, end=end, output_path=output,
        )
        return {"status": "ok", "rows_written": rows, "path": str(output), "start": start, "end": end}
    except Exception as exc:
        raise HTTPException(502, f"Deep history download failed: {exc}")


# ── AI research suggestions ───────────────────────────────────────────────────

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
                title="Strategy Lab — research suggestions ready",
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

## How this lab defines "good" (read carefully — it changed)
- Results are NET of 5bps/side costs with T+1 fills and realistic stop fills.
- EXCESS return (strategy minus buy-and-hold on the same bars) carries heavy \
scoring weight: a strategy that loses to simply holding the symbol is NOT a \
finding, whatever its absolute return. Most long-only timing on liquid index \
ETFs fails this bar — that is expected, not a bug.
- Every result is out-of-sample gated (train 70% / warm-indicator test 30%), \
and the most recent 15% of history is a held-out final exam nothing optimises \
against. Cross-symbol confirmation and bootstrap significance exist as extra \
filters. Suggest strategies that could survive ALL of that, not just fit.

## Research State ({datetime.now(timezone.utc).date().isoformat()})

Total experiments: {len(records)}
Grades: {dict(grades)}
Scoring: candidate ≥ 80, promising 65-79, watch 45-64, reject < 45

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
Be concrete — cite specific parameter values, thresholds, or data decisions. \
Do not repeat what has already been exhaustively tested in the results above."""


# ── market scanner ────────────────────────────────────────────────────────────

def _scanner_universe() -> list[str]:
    override = _env("SCANNER_UNIVERSE")
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]
    from strategy_lab.scanner import load_universe
    return load_universe()


def _scanner_csv() -> Path:
    return _private_storage() / "data" / "market_data" / _SCANNER_CSV_NAME


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
def evaluate_indicators(horizon: int = 5) -> dict:
    """Rank every indicator by predictive edge (Information Coefficient)."""
    from strategy_lab.indicator_eval import evaluate_all_indicators

    bars_by_symbol = _load_universe_bars()
    results = evaluate_all_indicators(bars_by_symbol, primary_horizon=horizon)
    return {
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
            title=f"Market Scanner — top {min(10, len(watchlist))} of {len(bars_by_symbol)}",
            message=f"Indicators: {used}\n\n{lines}\n\nResearch scan only — not trade advice.",
            priority="default",
        )
    return {
        "universe_size": len(bars_by_symbol),
        "watchlist": watchlist,
        "indicators_used": result["indicators_used"],
        "note": result.get("note"),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ── sync private state repo ───────────────────────────────────────────────────

@app.post("/sync-private")
def sync_private() -> dict:
    private = _private_state_repo()
    if not private.exists():
        raise HTTPException(
            500,
            f"Private state repo not found at {private}. "
            "Set STRATEGY_PRIVATE_STATE_REPO env var if cloned elsewhere.",
        )

    file_pairs = [
        (_EXPERIMENT_LOG, private / "data" / "experiments" / "experiment_log.jsonl"),
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
            ["git", "-C", str(private), "add",
             "data/experiments/experiment_log.jsonl",
             "data/runs/research_runs.jsonl",
             "reports/latest.md"],
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
            f"autonomous update — {date_str}\n\n"
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
        return {"status": "ok", "message": f"Synced and pushed — {date_str}"}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise HTTPException(500, f"Git error: {stderr}")
