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
        load_dotenv(env_file, override=False)
    except ImportError:
        pass  # python-dotenv not installed; caller must export env vars manually


_bootstrap_env()
from strategy_lab.backtest import build_signals
from strategy_lab.batch_runner import run_backtest_batch
from strategy_lab.data_loader import load_price_bars_from_csv
from strategy_lab.experiment_log import ExperimentLog
from strategy_lab.models import StrategySpec
from strategy_lab.reporting import top_records
from strategy_lab.run_ledger import ResearchRunLedger

# ── project layout ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_EXPERIMENT_LOG = _ROOT / "data" / "experiments" / "experiment_log.jsonl"
_RUN_LOG = _ROOT / "data" / "runs" / "research_runs.jsonl"
_REPORT = _ROOT / "reports" / "latest.md"
_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA"]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


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
    return {
        "total_experiments": len(records),
        "candidates": grades.get("candidate", 0),
        "promising": grades.get("promising", 0),
        "watch": grades.get("watch", 0),
        "rejects": grades.get("reject", 0),
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

        try:
            bars, _ = load_price_bars_from_csv(csv, symbol)
            closes = [bar.close for bar in bars]
            sig = build_signals(spec, closes)

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
            currently_long = False
            state = "error"
            last_date = None
            last_close = None

        results.append({
            "strategy": s["name"],
            "family": s["family"],
            "symbol": symbol,
            "score": record["score"],
            "grade": record["grade"],
            "parameters": s["parameters"],
            "signal": state,
            "currently_long": currently_long,
            "last_bar_date": last_date,
            "last_close": last_close,
        })

    active = [r for r in results if r["signal"] in ("entry", "exit")]
    return {
        "signals": results,
        "active_signals": active,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "last_bar_date": results[0]["last_bar_date"] if results else None,
    }


# ── run single symbol ─────────────────────────────────────────────────────────

class BatchRequest(BaseModel):
    symbol: str = "SPY"
    limit: int = 400
    purpose: str = ""


@app.post("/run-batch")
def run_batch(req: BatchRequest) -> dict:
    purpose = req.purpose or (
        f"Autonomous batch — {req.symbol} — {datetime.now(timezone.utc).date().isoformat()}"
    )
    try:
        result = run_backtest_batch(
            experiment_log_path=_EXPERIMENT_LOG,
            run_log_path=_RUN_LOG,
            report_path=_REPORT,
            purpose=purpose,
            limit=req.limit,
            data_csv=_data_csv(),
            symbol=req.symbol,
        )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── run all 4 symbols (main autonomous action) ────────────────────────────────

@app.post("/run-all")
def run_all(limit: int = 400) -> dict:
    csv = _data_csv()
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
    return {"runs": results, "timestamp": datetime.now(timezone.utc).isoformat()}


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
        f"return={r['metrics'].get('annualized_return_pct', 0):.1f}%, "
        f"drawdown={r['metrics'].get('max_drawdown_pct', 0):.1f}%, "
        f"sharpe={r['metrics'].get('sharpe_ratio', 0):.2f}, "
        f"trades={int(r['metrics'].get('trade_count', 0))}, "
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

Focus on achieving the first candidate-grade result (score ≥ 80). Be concrete — \
cite specific parameter values, thresholds, or data decisions. Do not repeat \
what has already been exhaustively tested in the results above."""


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
