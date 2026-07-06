# Strategy Lab — Debugging Guide (scar tissue catalog)

Every entry here is a failure that actually happened. Symptom → cause → fix.

## D1. /health or /status returns 500

Cause: a JSONL line in the hot log fails json.loads (every read endpoint
full-parses the log). Historically: OneDrive sync races (before relocation),
process killed mid-append, concurrent writers.
Fix: RUNBOOK "Corruption salvage". Check watchdog.log — check #4 names this
explicitly. Do NOT restart-loop the server; it can't fix data.

## D2. Batch "completed" notification but results look partial

Cause: the CLIENT timed out (Invoke-RestMethod TimeoutSec) while the SERVER
kept running — or a restart killed the batch mid-flight.
Diagnose: ledger tail (which symbols landed?) + lock file existence + whether
the hot-log line count still grows over 10 seconds.
Fix: if the server is still running, wait. If genuinely dead: clear the stale
lock, verify tail integrity, re-run the same call — fingerprint dedup makes
re-runs safe and cheap (completed work is skipped).

## D3. Two writers corrupted/duplicated records

Causes seen: (a) scheduled n8n run overlapping a manual batch (pre-lock era);
(b) THE STALE-RECLAIM FLAW: lock reclaim threshold (2h) < long batch duration
(24h backfill), so a second caller "reclaims" a live lock. Incident
2026-07-06: prune ran concurrently for ~30s; ~1-2 records lost; log stayed
clean.
Mitigate now: before long batches, place a guard: a fresh lock file plus a
loop touching it every 20 min while the ledger stays active (pattern:
C:\StrategyLabData\lock_guard.ps1).
Fix properly (SANCTIONED pending change): heartbeat — the batch touches the
lock file between symbols; reclaim only when mtime is genuinely old (>3h).

## D4. Every fingerprint suddenly "new"; the grid re-runs by itself

Cause: dataset.end moved (a data refresh) OR ENGINE_VERSION changed OR
someone re-downloaded Yahoo data over existing symbols (adjusted history
mutates). dataset.end is part of the fingerprint.
Fix: vintages pin end dates — check
C:\StrategyLabData\experiments\dataset_vintage.json. If an unintended advance
happened, restore the previous pin; duplicate-vintage records already created
are harmless clutter.

## D5. Results look TOO good

Checklist, in order of historical frequency:
1. Which dataset? Split-only (default/scanner) overstates excess ~1.5-2%/yr.
2. /significance — p near 0.5 = luck. 30k+ trials guarantee lucky outliers.
3. /regime-report — all the profit in one year/era? (gap_pct flipped sign
   between 2022+ and 2005+ data; momentum rotation +4%/yr but OOS-dead.)
4. Off-grid extreme params (the exit_rsi=86 syndrome) = in-sample drift.
5. Did someone weaken a gate? Diff scoring.py/batch_runner.py against main.

## D6. PowerShell weirdness

- Regex on .env never matches: unicode/BOM in an ANSI-read .ps1 — keep
  watchdog and ALL .ps1 files PURE ASCII; match without a `^` anchor (line 1
  carries a BOM).
- Multiline commit messages explode into "pathspec" errors: write the message
  to a scratch file and use `git commit -F`.
- `&&` is a parse error (PS 5.1). Chain with `;` or gate with
  `if ($LASTEXITCODE -eq 0)`.
- `claude` CLI is not on PATH in this desktop environment; MCP registration
  goes through ~/.claude.json (with a backup) instead.

## D7. Python/env drift

- Bare `python` = Inkscape. Always `py -3.12`.
- pydantic/pydantic-core mismatch once crashed fresh imports (shared global
  Python) while the RUNNING server stayed healthy — the next restart was a
  landmine. After ANY dependency change verify in a fresh process:
  `py -3.12 -c "import strategy_lab.server"`.

## D8. n8n workflow import fails or nodes vanish

- "unknown_connection_source": you renamed a node; `connections` are keyed by
  node NAME — update the connections block too.
- CLI import needs a top-level "id"; import does not activate — run
  `n8n update:workflow --id=<id> --active=true` and VERIFY by exporting back
  (imports have silently not-applied when the JSON was invalid).
- Timezone: per-workflow settings.timezone. The container is shared with
  other projects: never set TZ globally.

## D9. Auto-research proposes garbage / errors every round

- Portfolio records seeding single-symbol refinement (excluded via
  PORTFOLIO_STRATEGY_NAMES — add any new portfolio family there).
- Param bounds: RSI-like params must stay in (1,99); add new bounded
  parameters to PARAM_BOUNDS or the climber walks through the ceiling.

## D10. Test-fixture traps (cost hours twice)

- "Impossible" RSI thresholds aren't: RSI pins to 0 on monotonic declines and
  100 on rallies. For a provably-flat sleeve use a lookback longer than the
  data.
- sma() returns values[index] during warmup (it blocks entries; it is not None).
- Synthetic calendar-day bars: warmup drag lands in year one of
  yearly_breakdown — assert per-year, not aggregate.
