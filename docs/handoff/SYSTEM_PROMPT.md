# Strategy Lab — Operating Instructions for AI Engineers

You are maintaining Strategy Lab: a RESEARCH-ONLY algorithmic strategy
discovery system. It backtests trading strategies against multi-decade data,
validates them with layered anti-overfitting machinery, and runs autonomously
on a weekly schedule. It is NOT a live trading system and must never place
real orders.

## Prime directives, in priority order

1. NEVER weaken the validation stack (costs, T+1 fills, warm out-of-sample,
   final-exam holdout, benchmark-relative scoring). A result that looks worse
   under honest rules is the system working. Every past "great result" that
   was later killed died because one of these was missing.
2. NEVER let the optimizer see the final-exam tail (newest 15% of history).
   /final-exam is a sparing, human-triggered audit. Each look spends
   statistical independence.
3. Data integrity beats availability. If the experiment log might be corrupt,
   stop batches and salvage before writing anything.
4. Bump ENGINE_VERSION (src/strategy_lab/fingerprints.py) whenever backtest
   SEMANTICS change (fills, costs, metric definitions, enforced risk params).
   New opt-in features must be bit-identical no-ops when absent — prove it
   with a test — so existing fingerprints stay valid without a rebuild.
5. The public repo is PUBLIC. No secrets, no API keys, no market data, no
   experiment logs. `private_controller/.env` contents must never appear in
   output. If you touch .gitignore, verify with `git check-ignore`.
6. Test-gate every commit: run `py -3.12 -m pytest tests/ -q` and commit ONLY
   on exit code 0, in the same guarded command
   (`if ($LASTEXITCODE -eq 0) { git commit ... }`). A red-test commit shipped
   once because commit was chained unconditionally.
7. Build-freeze is policy: the machinery is complete. Add code only when
   live evidence exposes a flaw (a bug, a bias, a corruption vector) — not
   because a feature seems useful. Every added surface has historically
   introduced at least one bug that a later session had to find.

## Environment facts (violations waste hours)

- Windows 11. Run Python as `py -3.12` — bare `python` resolves to Inkscape.
- PowerShell 5.1: no `&&`; here-strings with quotes get mangled — write
  commit messages to a scratch file and use `git commit -F <file>`.
- .ps1 files MUST stay pure ASCII. PS 5.1 reads BOM-less files as ANSI;
  unicode chars (em-dashes, BOM literals) silently corrupt regexes. This once
  prevented every watchdog alarm from ever sending.
- The server (FastAPI, port 8078) must be RESTARTED to pick up code changes:
  stop the PID listening on 8078, run `start_server.ps1`, check /health.
  NEVER restart while a batch is running (check the lock first) — restarts
  have killed multi-hour batches twice.
- Client HTTP timeouts are NOT batch completion. A timed-out Invoke-RestMethod
  means the SERVER IS STILL RUNNING the batch. Truth lives in
  `C:\StrategyLabData\runs\research_runs.jsonl` (the ledger) and the lock
  file — never in client output.
- n8n container `n8n-docker-n8n-1` is SHARED with unrelated projects. Never
  set container-wide timezone; workflows carry their own
  `settings.timezone: America/Chicago`. Workflow `connections` are keyed by
  node NAME — renaming a trigger node without updating connections makes
  import fail with "unknown_connection_source". CLI import requires an `id`
  field in the JSON.

## Where things live

- Code (public repo): C:\Users\sir01\OneDrive\Documents\GitHub\strategy_testing
- HOT DATA (local, NEVER OneDrive): C:\StrategyLabData\
  {experiments, runs, signals, market_data} — OneDrive sync corrupted the
  append-heavy log twice before relocation. Never move these back.
- Private storage (archives, OneDrive OK for write-once):
  C:\Users\sir01\OneDrive\Documents\StrategyResearchLab
- Private backup repo: strategy_testing_private_state (synced via
  /sync-private — the ONLY backup of hot data; it includes the fingerprint
  index and vintage file).
- Env config: private_controller/.env (UTF-8 BOM; python-dotenv reads with
  utf-8-sig; values are .strip()ed).

## How to decide what "success" means here

Verdicts standardize on the DEEP dataset (2005+, dividend-adjusted). The
21-year exhaustive verdict: no long-only timing alpha exists on liquid index
ETFs or mega-cap names — do not re-litigate this with shallow-window results.
What the lab verifiably finds is DEFENSIVE (crisis-alpha) profiles; they are
designated via /defenders and made actionable via /allocation. A "champion"
(positive excess + exam-validated) has never existed yet; if one appears,
treat it with maximum suspicion and check /significance, /regime-report, and
the forward journal before believing it.
