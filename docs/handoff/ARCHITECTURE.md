# Strategy Lab — Architecture & Design Rationale (handoff)

Note: docs/architecture.md is the ORIGINAL scaffold document and predates the
engine rebuilds; where they conflict, THIS document is authoritative.

## Data flow (one sentence)

Grids/auto-research propose StrategySpecs → backtest engine simulates honestly
→ scoring + layered validation grade them → append-only experiment log with
fingerprint dedup → analysis/designation endpoints read the log → n8n schedules
the whole loop → ntfy reports to the human.

## Modules (src/strategy_lab/)

| Module | Role | Non-obvious rationale |
|---|---|---|
| backtest.py | Single-symbol long/flat engine + all signal families | T+1 entry (signal bar ≠ action bar); stop fills at stop price/open-on-gap, never the recovered close; profit target as resting limit; vol targeting from trailing data only; `assemble_metrics` includes benchmark & excess. Opt-in risk params are exact no-ops when absent. |
| portfolio_backtest.py | Switch engine v1: hold one-of-N-or-cash | Per-leg costs on switches; benchmark = the risk leg; bond_low uses STRICT new-low (`<=` including today reads a flat series as "always at low"). |
| fingerprints.py | SHA of engine_version+name+params+risk+dataset | Prose (hypothesis/rules) EXCLUDED — including it once orphaned the whole log on a rewording. ENGINE_VERSION history is the changelog of semantics. |
| batch_runner.py | Evaluate+log loop; OOS validation; final-exam trim | OOS = train 70% score vs WARM held-out 30% (`run_backtest_window`: signals from full history, simulate the slice — cold-start warmup once left sma_filter=200 combos gated on 7 trades). OOS failure keys on held-out SCORE not grade (trade-count hard-reject is miscalibrated for short windows). FINAL_EXAM_FRACTION=0.15 trimmed before EVERYTHING. |
| scoring.py + configs/research_criteria.yaml | Weighted 0-100 + grades | excess_return_pct carries 0.15 weight: losing to buy-and-hold is not a finding. |
| experiment_log.py | Append-only JSONL + sidecar .fingerprints.idx + prune | Index = permanent memory of everything ever run; prune archives rejects but NEVER removes idx entries (pruned combos must not re-run). Atomic temp+os.replace rewrite. |
| auto_research.py | Bounded hill-climber + exploration quota | Seeds MUST be OOS-eligible (evaluated, oos>=45, oos_trades>=5, non-reject), family-capped (2), ranked by min(stability, oos) = weakest evidence — ranking on in-sample score alone caused the exit_rsi 65→86 drift. 25% exploration budget. objective=defensive subtracts drawdown points. Portfolio names excluded (their children crash the single-symbol engine). |
| analysis.py | Stability (grid-adjacent neighbors), cross-symbol support | List params normalized to tuples (portfolio records crashed hashing). |
| defenders.py | Crisis-alpha designation + blend/allocation sweeps | The lab's actual product. Bar: OOS>=45, dd<=half benchmark, wins >=2/3 of benchmark down-years. |
| signal_journal.py | Forward walk-forward record + external signals | Idempotent per bar-date; evidence written BEFORE the future happens — immune to all backtest overfitting. |
| data_loader.py | CSV loader with end_cap + VINTAGE system | dataset.end is fingerprinted → refreshes would rotate every fingerprint. Vintages pin the research end-date; only /advance-vintage moves it (deliberate, quarterly). |
| yahoo_data.py | Deep 2005+ dividend-adjusted data | append_new_only preserves existing rows BYTE-FOR-BYTE: Yahoo recomputes adjusted history continuously; re-downloading a symbol silently changes data under old fingerprints. |
| alpaca_data.py | Recent daily data | adjustment="split" is mandatory (default raw gave NVDA a phantom -90% split day). Split-only means excess is ~1.5-2%/yr OPTIMISTIC for low-exposure strategies (missing dividends hit the benchmark hardest). Deep is the honest dataset. |
| server.py | All endpoints + batch write-lock | Lock (data root .batch.lock, O_CREAT|O_EXCL) wraps every log-append path. KNOWN FLAW: 2h stale-reclaim < long batches (24h backfill) — a heartbeat fix is the sanctioned pending change. |

## Datasets

| key | source | span | adjustment | use |
|---|---|---|---|---|
| default | Alpaca IEX | 2020+ | split-only | fast iteration; excess biased optimistic |
| deep | Yahoo | 2005+ | split+dividend | ALL VERDICTS; ETFs + 12 single names |
| scanner | Alpaca | 2022+ | split-only | cross-sectional IC + watchlist |
| portfolio | Yahoo | 2005+ | split+dividend | switch strategies (bond yields matter) |

## Autonomy (all timezones America/Chicago, per-workflow)

- Mon 07:00: health → refresh → run-all(default) → deep fill → scanner-deep fill → suggest → sync
- Tue 07:00: AR default → AR deep → portfolio → prune → champions → defenders → journal-drift → sync
- Mon-Fri 08:30: /signals (journals daily); 15:15: /scan
- Daily 08:00: watchdog.ps1 (self-heals server once, freshness+integrity, ntfy)
- Task Scheduler: "Strategy Lab Server" (logon), "Strategy Lab Watchdog" (daily)
