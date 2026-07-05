# Strategy Lab autonomy watchdog.
# Runs OUTSIDE the server (daily scheduled task) so it can detect the server
# being down - an in-server health check cannot report its own death.
#
# NOTE: this file must stay PURE ASCII. PowerShell 5.1 reads BOM-less .ps1 as
# ANSI, so any unicode character (em-dash, box-drawing, BOM literals in
# regexes) silently corrupts patterns - that bug prevented ntfy alarms from
# ever sending until 2026-07-04.
#
# Checks:
#   1. /health responds. If not: attempt ONE restart via start_server.ps1,
#      re-check, and alert either way (recovered vs still down).
#   2. Newest research-run ledger entry is < 8 days old.
#   3. Market data is < 14 days old.
#   4. Experiment log tail parses as JSON (corruption named explicitly).
#
# Silent on success. Alerts via ntfy on any failure. Never prints .env contents.
$ErrorActionPreference = "SilentlyContinue"
$root = $PSScriptRoot
$log = Join-Path $root "logs\watchdog.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $log
}

function Get-EnvValue($envFile, $name) {
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*#') { continue }
        # No start anchor: the first line may carry a UTF-8 BOM.
        if ($line -match "$name\s*=\s*(.+)$") { return $Matches[1].Trim() }
    }
    return $null
}

$envFile = Join-Path $root "private_controller\.env"
$ntfyTopic = Get-EnvValue $envFile "NTFY_TOPIC"

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

# -- 1. server health (with one self-heal attempt) ----------------------------
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

# -- 2. research-run freshness -------------------------------------------------
$dataDir = Get-EnvValue $envFile "STRATEGY_DATA_DIR"
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
    $problems += "Research run ledger is missing at $runLog."
}

# -- 3. market-data freshness ---------------------------------------------------
$marketDir = Get-EnvValue $envFile "STRATEGY_MARKET_DATA_DIR"
$storageRoot = Get-EnvValue $envFile "STRATEGY_PRIVATE_STORAGE_ROOT"
if (-not $marketDir -and $storageRoot) { $marketDir = Join-Path $storageRoot "data\market_data" }
if ($marketDir) {
    $csv = Join-Path $marketDir "alpaca_iex_etfs.csv"
    if (Test-Path $csv) {
        $dataAge = ((Get-Date) - (Get-Item $csv).LastWriteTime).TotalDays
        if ($dataAge -gt 14) {
            $problems += "Market data is $([math]::Round($dataAge,0)) days old - /refresh-data is not running."
        }
    } else {
        $problems += "Market data CSV not found in private storage."
    }
}

# -- 4. experiment-log integrity ------------------------------------------------
$expLog = Join-Path $dataDir "experiments\experiment_log.jsonl"
if (Test-Path $expLog) {
    $tail = Get-Content $expLog -Tail 3 | Where-Object { $_.Trim() }
    foreach ($line in $tail) {
        try { $null = $line | ConvertFrom-Json -ErrorAction Stop }
        catch {
            $problems += "Experiment log tail is CORRUPT (invalid JSON) - run a salvage before the next batch."
            break
        }
    }
}

# -- 5. vintage age (informational nudge, not an alarm) ---------------------------
$vintageFile = Join-Path $dataDir "experiments\dataset_vintage.json"
if (Test-Path $vintageFile) {
    $vintageAge = ((Get-Date) - (Get-Item $vintageFile).LastWriteTime).TotalDays
    if ($vintageAge -gt 100) {
        Write-Log "INFO: dataset vintages are $([math]::Round($vintageAge,0)) days old - consider POST /advance-vintage (quarterly cadence; rotates fingerprints deliberately)."
    }
}

# -- report ----------------------------------------------------------------------
if ($problems.Count -gt 0) {
    Send-Alarm "Strategy Lab watchdog: $($problems.Count) problem(s)" ($problems -join "`n")
} else {
    Write-Log "all checks passed"
}
