# Strategy Lab — Extension Protocols & Research Ledger

## Adding a single-symbol strategy family

1. Implement in backtest.py: close-only → `build_signals`; needs OHLCV/dates
   → `build_signals_from_bars`. Honor max_hold_days via risk_model; leave
   stops/targets/vol to the shared exit machinery (do NOT reimplement).
2. Grid in configs/experiment_space.yaml — SMALL (tens of combos). No zero
   values duplicating "key absent" baselines (absent IS the baseline).
3. Tests: metrics-shape plus at least one BEHAVIORAL test (the bollinger
   "snap-back not falling-knife" test caught a real logic error).
4. Update ALL_STRATEGY_NAMES in tests/test_experiment_generator.py.
5. NO ENGINE_VERSION bump (a new family = new fingerprints; nothing existing
   is invalidated).
6. Suite green → commit → RESTART server → the next /run-all picks it up on
   every dataset automatically.

## Adding a risk lever (stop/target/sizing class)

- MUST be an exact no-op when absent — bit-identical outputs, proven by a
  test (`plain == with_zero`). Then: no version bump, no rebuild.
- Realistic fills only: decide with intrabar data → fill at the trigger price
  or worse (open on gaps), NEVER at a recovered close. Same-bar conflicts
  resolve pessimistically (stop beats target).
- Thread through ALL FIVE call sites: run_backtest, run_backtest_window,
  backtest_trades, yearly_breakdown, defenders._strategy_daily_returns.
  Missing one silently desyncs OOS/exam/significance from the main metrics.
- Add PARAM_BOUNDS if bounded. Grid: a single probe value first.

## Changing engine semantics (fills, costs, metrics, OOS logic)

1. Bump ENGINE_VERSION with a one-line history comment (fingerprints.py).
2. Full rebuild per RUNBOOK (archive → reset → re-run). Expect scores to
   CHANGE and probably DROP; that is the point.
3. Never mix engines in the hot log — the analysis stack will happily rank
   stale-engine records above honest ones.

## Adding a dataset

- New CSV name = new dataset identity (fingerprints key on it).
- Wire: _select_csv, vintage auto-init, /run-all //run-batch reachability,
  and the stem_to_dataset maps in /defenders and /allocation.
- Use dividend-adjusted sources for anything verdicts depend on.
- Yahoo-sourced data: append-new-only forever (adjusted history mutates).

## Adding an endpoint

- Log-append paths MUST hold _batch_write_lock. Read paths must not.
- Per-strategy loops: try/except per item and surface the error string — a
  bare swallow once made broken strategies look permanently "flat".
- Costly analysis (top_robust_records is O(m^2) per group) belongs AFTER a
  prune.
- Add a hermetic TestClient test (tests/test_server_endpoints.py pattern:
  monkeypatch the module path globals to tmp).

---

# RESEARCH LEDGER — settled verdicts (do not re-litigate on weaker evidence)

| Verdict | Evidence | Implication |
|---|---|---|
| No long-only timing alpha on SPY/QQQ/IWM/DIA daily bars | ~30k combos x 21 years, honest engine, 0 positive-excess survivors | Index-ETF "beat the market" claims from any source start presumed false |
| Same for mega-cap single names (AAPL/MSFT/NVDA/AMZN) | 21y each: best excess -10..-20%/yr | Hyper-compounders are unbeatable benchmarks by construction |
| The lab finds DEFENSIVE profiles | rsi_pullback/DIA: -1.4% in 2008 vs -32%, 7% dd over 21y; 6 validated defenders; every 50/50 blend improved Sharpe | The product is drawdown reduction; see /defenders + /allocation |
| Mean reversion is the persistent effect | Deep IC: bollinger %B -0.065, RSI -0.064 (2-4x the shallow-window read) | Prior for new families; validated bollinger_reversion |
| Gap continuation is era-fragile | IC +0.031 (2022+) vs -0.012 (2005+) | Shallow-window edges are presumed artifacts until deep-tested |
| Asset-class momentum: +4%/yr over 21y BUT OOS-decayed (oos 33) | First positive-excess result ever; rejected by the gate | Famous factors decay; the OOS gate exists precisely for this |
| Exit-study tension | Davey's 250k-curve study: targets beat stops; our stop lever helped mean reversion modestly | Context-dependent; the profit_target_atr lever exists to test per-family |

## Pending items a successor inherits

1. DONE 2026-07-06 (commit 33ed916): lock heartbeat shipped. Stale-reclaim
   window is now 15 min (was 2h); batches heartbeat once per EXPERIMENT via
   `_batch_write_lock()`'s yielded callable, threaded through every batch
   entry point. NOT YET ACTIVE as of this writing — lands at the next server
   restart after the running deep single-name backfill completes (never
   restart the server while `_batch_write_lock` is held).
2. Deep single-name backfill: 11 of 12 names complete as of 2026-07-06
   (missing: XLF, in progress). Read the verdict with /regime-report +
   /defenders, not just scores.
3. Human's one click: TradingView Pine editor → "LabX Bollinger Reversion" →
   Add to chart → then data_get_strategy_results enables cross-engine
   validation (TV MCP: streamable HTTP, 127.0.0.1:8100/mcp, 81 tools).
4. The journal matures with calendar time; /journal-drift verdicts become
   meaningful after ~3 months. If a defender survives that, the next PHASE
   decision (human's) is routing it to the existing Alpaca paper-trading
   infrastructure.
5. Quarterly: /advance-vintage per dataset (watchdog nudges at 100 days).
6. FIXED 2026-07-07 (see docs/handoff/DEBUGGING.md D11 and
   FIX_BRIEF_watchdog_fratricide.md for the original diagnosis): both "silent
   server deaths" were the WATCHDOG killing a healthy-but-busy server —
   /health used to parse the whole 85MB log, exceeding the 20s probe timeout
   under batch load, and the "self-heal" Stop-Process executed the process.
   Memory hypothesis REFUTED (peak WS 489MB / 30GB machine; zero crash events
   in Windows Event Log). Implemented: /health is now O(1) (hot_log_bytes +
   batch_running, no log parse, ever); watchdog.ps1 uses a busy-vs-dead
   ladder (Get-NetTCPConnection + batch-lock mtime before ever restarting);
   start_server.ps1 refuses to run its Stop-Process block when a fresh lock
   exists (bypass with -Force). 3 new tests (117 total). NOT YET ACTIVATED —
   like the heartbeat fix, this lands at the next safe server restart (no
   batch lock held); watchdog.ps1 and start_server.ps1 changes are live
   immediately since they run as separate processes, but /health's new
   response shape needs the server process itself restarted.
