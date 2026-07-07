# FIX BRIEF: Watchdog fratricide (the "silent server deaths")

Status: DIAGNOSED 2026-07-06. Ready for implementation. Read this whole brief
before touching code; then follow docs/handoff/SYSTEM_PROMPT.md rules
(test-gate commits, pure-ASCII .ps1, no server restart while the batch lock
is held).

## Root cause (evidence-backed, do not re-investigate)

Both "silent server deaths" (2026-07-04 23:42, 2026-07-06 08:00) were the
watchdog killing a healthy, busy server:

1. /health calls ExperimentLog.records() - a full parse of the (currently
   85MB) experiment log - just to report len(records).
2. Under batch CPU load that parse exceeds the watchdog's 20s health timeout.
3. Watchdog concludes "down" and runs start_server.ps1, which begins with
   Stop-Process -Force on the port-8078 PID.
4. External kill = no traceback. Windows Event Log confirms: zero python
   crash records at either timestamp. Memory is exonerated (peak WS 489MB on
   a 30GB machine).

The heartbeat fix (commit 33ed916) is a perfect complement: a fresh batch
lock mtime is now a reliable "alive and working" signal the watchdog can use.

## Fix 1 - server.py: make /health O(1)

/health must be pure liveness: NEVER construct ExperimentLog or read the log.
Replace the body with cheap facts only:

- "status": "ok"
- "timestamp": utc now
- "hot_log_bytes": os.stat size of _EXPERIMENT_LOG (0 if missing)
- "batch_running": _BATCH_LOCK.exists()
- keep "data_csv_exists", "openrouter_configured", "alpaca_configured"
  (all cheap)
- REMOVE "total_experiments" from /health (it stays in /status, which is
  allowed to be expensive).

CHECK before removing: the Monday n8n workflow's "Server Up?" IF node -
confirm it only tests the status field / HTTP success, not total_experiments
(n8n/strategy_research_workflow.json). Adjust the IF node if needed and
re-import + reactivate the workflow (remember: connections keyed by node
name; import then update:workflow --active=true; verify by export).

Test (tests/test_server_endpoints.py, hermetic fixture already exists):
write INVALID JSON into the experiment log fixture, then assert GET /health
is 200 while GET /status is 500. That single test encodes the whole lesson:
liveness must not depend on data validity or size. Also assert batch_running
flips when the lock fixture file exists.

## Fix 2 - watchdog.ps1: never kill a live process

Current check 1 logic: /health (20s) fails -> run start_server.ps1 (which
kills). Replace with this decision ladder (KEEP THE FILE PURE ASCII - see
DEBUGGING.md D6):

1. GET /health with TimeoutSec 60 (was 20). Success -> healthy, done.
2. On failure: is anything LISTENING on 8078? (Get-NetTCPConnection)
   - Nothing listening -> genuinely dead -> start_server.ps1, re-check,
     alarm "was DOWN, restarted" (current behavior, now correctly scoped).
   - Something listening -> DO NOT KILL. Check the batch lock
     (C:\StrategyLabData\experiments\.batch.lock via STRATEGY_DATA_DIR):
       - lock exists and mtime < 15 min old (matches the heartbeat) ->
         server is BUSY on a live batch. Write-Log INFO, no alarm, no
         restart. This is the exact scenario that caused both deaths.
       - no fresh lock -> retry /health once after 30s; if still failing,
         ALARM "server listening but unresponsive - investigate manually"
         and DO NOT restart. A hung-but-listening process is for a human;
         automated killing is what caused this incident class.

## Fix 3 - start_server.ps1: refuse to kill a working batch (defense in depth)

Before its Stop-Process block: if the batch lock exists with mtime < 15 min,
print a refusal message and exit 1 instead of killing, unless invoked with
-Force. (Param: [switch]$Force at top.) The logon-trigger use case is
unaffected: at logon there is no live server, so no fresh lock. Keep ASCII.

## Rollout order

1. Implement + test all three (suite must stay green; 115 tests currently).
2. Commit (test-gated), push.
3. DO NOT restart the server if the batch lock is held (the deep backfill may
   still be running). Activation happens at the next natural safe restart -
   which also activates the heartbeat fix from 33ed916.
4. After activation: run watchdog.ps1 manually once DURING a running batch
   and confirm the log shows the INFO path, not a restart.
5. Update docs/handoff/DEBUGGING.md: add entry D11 (this incident class) and
   correct EXTENDING.md pending item 6 (mystery solved: not memory - the
   memory-profile suggestion is withdrawn).

## Definition of done

- /health returns in <100ms regardless of log size and returns 200 even when
  the log is corrupt.
- Watchdog run during a live batch: INFO logged, server untouched.
- Watchdog run with the server actually dead (no listener): restart + alarm,
  as before.
- start_server.ps1 without -Force refuses when a fresh lock exists.
- All existing tests green plus the new ones; docs updated.
