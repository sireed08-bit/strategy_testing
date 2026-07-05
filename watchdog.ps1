# Strategy Lab autonomy watchdog.
# Runs OUTSIDE the server (daily scheduled task) so it can detect the server
# being down — an in-server health check cannot report its own death.
#
# Checks:
#   1. /health responds. If not: attempt ONE restart via start_server.ps1,
#      re-check, and alert either way (recovered vs still down).
#   2. The newest research-run ledger entry is < 8 days old (the weekly
#      grid-fill + auto-research cadence should never leave a gap that long).
#   3. Market data is < 14 days old.
#
# Silent on success. Alerts via ntfy on any failure. Never prints .env contents.
$ErrorActionPreference = "SilentlyContinue"
$root = $PSScriptRoot
$log = Join-Path $root "logs\watchdog.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $log
}

# Read NTFY_TOPIC from the private .env without exposing anything else.
$ntfyTopic = $null
$envFile = Join-Path $root "private_controller\.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^﻿?NTFY_TOPIC\s*=\s*(.+)$') { $ntfyTopic = $Matches[1].Trim(); break }
    }
}

function Send-Alarm($title, $message) {
    Write-Log "ALARM: $title - $message"
    if ($ntfyTopic) {
        try {
            Invoke-RestMethod -Uri "https://ntfy.sh/$ntfyTopic" -Method POST -Body $message `
                -Headers @{ Title = $title; Priority = "high" } -TimeoutSec 15 | Out-Null
        } catch { Write-Log "ntfy send failed: $($_.Exception.Message)" }
    }
}

$problems = @()

# ── 1. server health (with one self-heal attempt) ─────────────────────────────
$healthy = $false
try {
    $h = Invoke-RestMethod "http://127.0.0.1:8078/health" -TimeoutSec 20
    $healthy = ($h.status -eq "ok")
} catch { $healthy = $false }

if (-not $healthy) {
    Write-Log "server down - attempting restart"
    & (Join-Path $root "start_server.ps1") | Out-Null
    Start-Sleep -Seconds 12
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:8078/health" -TimeoutSec 20
        $healthy = ($h.status -eq "ok")
    } catch { $healthy = $false }
    if ($healthy) {
        $problems += "Server was DOWN and was auto-restarted successfully. Check why it died (logs/server_error.log)."
    } else {
        $problems += "Server is DOWN and auto-restart FAILED. The lab is not running."
    }
}

# ── 2. research-run freshness ─────────────────────────────────────────────────
# Honor STRATEGY_DATA_DIR (hot data relocated out of OneDrive after sync-race
# corruption); fall back to the in-repo path.
$dataDir = $null
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^﻿?STRATEGY_DATA_DIR\s*=\s*(.+)$') { $dataDir = $Matches[1].Trim(); break }
    }
}
if (-not $dataDir) { $dataDir = Join-Path $root "data" }
$runLog = Join-Path $dataDir "runs\research_runs.jsonl"
if (Test-Path $runLog) {
    $lastLine = Get-Content $runLog -Tail 1
    $lastRun = $null
    if ($lastLine -match '"created_at":\s*"([0-9T:\-\.\+]+)') { $lastRun = [datetime]$Matches[1] }
    if (-not $lastRun) { $lastRun = (Get-Item $runLog).LastWriteTime }
    $ageDays = ((Get-Date).ToUniversalTime() - $lastRun.ToUniversalTime()).TotalDays
    if ($ageDays -gt 8) {
        $problems += "No research run in $([math]::Round($ageDays,1)) days - the weekly schedule is not firing."
    }
} else {
    $problems += "Research run ledger is missing entirely."
}

# ── 3. market-data freshness ──────────────────────────────────────────────────
$storageRoot = $null
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^﻿?STRATEGY_PRIVATE_STORAGE_ROOT\s*=\s*(.+)$') { $storageRoot = $Matches[1].Trim(); break }
    }
}
if ($storageRoot) {
    $csv = Join-Path $storageRoot "data\market_data\alpaca_iex_etfs.csv"
    if (Test-Path $csv) {
        $dataAge = ((Get-Date) - (Get-Item $csv).LastWriteTime).TotalDays
        if ($dataAge -gt 14) {
            $problems += "Market data is $([math]::Round($dataAge,0)) days old - /refresh-data is not running."
        }
    } else {
        $problems += "Market data CSV not found in private storage."
    }
}

# ── report ────────────────────────────────────────────────────────────────────
if ($problems.Count -gt 0) {
    Send-Alarm "Strategy Lab watchdog: $($problems.Count) problem(s)" ($problems -join "`n")
} else {
    Write-Log "all checks passed"
}
