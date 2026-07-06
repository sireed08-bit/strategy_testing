# Strategy Lab — Operations Runbook

## Health check (first command of any session)

```powershell
Invoke-RestMethod "http://127.0.0.1:8078/status" -TimeoutSec 30
# expect: total_experiments = hot_log_records + archived_rejects (honest math)
Test-Path C:\StrategyLabData\experiments\.batch.lock   # True = batch running
Get-Content C:\StrategyLabData\runs\research_runs.jsonl -Tail 3  # ground truth
```

Slow /status or timeouts under load = a batch is grinding. That is normal.

## Restart the server (ONLY when no batch is running)

```powershell
if (Test-Path C:\StrategyLabData\experiments\.batch.lock) { throw "BATCH RUNNING - do not restart" }
$p = (Get-NetTCPConnection -LocalPort 8078 -State Listen -ErrorAction SilentlyContinue).OwningProcess
foreach ($id in $p) { Stop-Process -Id $id -Force }
Set-Location <repo>; & .\start_server.ps1; Start-Sleep 8
Invoke-RestMethod http://127.0.0.1:8078/health
```

Restarting mid-batch kills it silently: the client keeps "waiting", the lock
may go stale, and you will misread a later notification as completion. This
mistake was made twice. Check the lock. Then check it again.

## Run research manually

- Grid fill: `POST /run-all?limit=8000&dataset=deep` (per-symbol; dedup skips known)
- Single names: `POST /run-scanner-batch?dataset=deep&limit=8000` (~2h/symbol on deep!)
- Refinement: `POST /auto-research?dataset=deep&objective=defensive`
- Portfolio: `POST /run-portfolio`

Rules: background long calls; set client TimeoutSec above the worst case
(deep single-name full grid is ~2h/symbol); NEVER trust client output for
results — read the ledger. The write-lock serializes all of these; a 409
means wait.

## Weekly-cycle readouts (what the human actually wants)

1. /top-results, /robust-results — raw and stability-ranked leaders
2. /champions — positive-excess + exam-validated (has never fired; suspect it if it does)
3. /defenders and /allocation — the actionable output (blend weights)
4. /regime-report?dataset=deep — year-by-year: did it earn only in one era?
5. /significance — could the trades be luck? (filter, not proof)
6. /journal, /journal-drift — forward record vs backtest expectation

## Prune (keep the hot log fast)

`POST /prune` archives reject-grade to private storage; the fingerprint index
keeps ALL entries so pruned combos never re-run. Run after big batches. The
Tuesday workflow does it automatically.

## Full rebuild (only after an ENGINE_VERSION bump)

1. Verify no batch running (lock).
2. Archive: copy experiment_log.jsonl + .fingerprints.idx + .meta.json from
   C:\StrategyLabData\experiments to
   `<private storage>\archive\engine_vN_YYYY-MM-DD\`
3. `Clear-Content` the log; DELETE the idx and meta (fresh engine = fresh space).
4. Restart server; `POST /run-all?limit=<full grid>` per dataset (background).
5. Prune, sync, verify /status math.

## Corruption salvage (watchdog alarms "log tail CORRUPT" or /health 500s)

1. STOP: no batches, no restarts until diagnosed.
2. Scan: parse every line as JSON; count bad; keep first-seen per fingerprint.
3. Rewrite via temp file + os.replace. Rebuild idx = valid idx lines UNION
   salvaged fingerprints (idx is an append-only superset; NEVER shrink it).
4. Record how many rows were lost; their fingerprints remain in idx (those
   combos are marked done forever — acceptable, note it).

## Vintage advancement (quarterly, deliberate, human-approved)

`POST /advance-vintage?dataset=deep|default|scanner|portfolio` moves the
pinned research end-date to the CSV's current max. EVERY fingerprint for that
dataset rotates; the grid re-runs on subsequent batches. Never do this as a
side effect; the watchdog nudges after 100 days.

## Data refreshes

- /refresh-data: Alpaca ETFs (skips if <7 days old). Safe: vintage-pinned.
- /refresh-deep-data & /refresh-portfolio-data: Yahoo, APPEND-NEW-ONLY by
  default. Passing append=false re-downloads everything = silently different
  adjusted history under unchanged fingerprints. Only with a vintage advance.

## Backups

/sync-private pushes log+idx+meta+vintage+runs+journal+report to the private
repo. This is the ONLY backup of C:\StrategyLabData. If sync starts failing,
treat as urgent.
