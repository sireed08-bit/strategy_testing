# Strategy Lab Server - auto-start script
# Registered with Windows Task Scheduler to run at user logon.
# Starts uvicorn as a hidden background process; logs go to logs\server.log

param(
    [switch]$Force
)

$projectRoot = "C:\Users\sir01\OneDrive\Documents\GitHub\strategy_testing"
$logDir = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$logFile  = Join-Path $logDir "server.log"
$errFile  = Join-Path $logDir "server_error.log"

# Defense in depth (see docs/handoff/FIX_BRIEF_watchdog_fratricide.md): refuse
# to kill a process that is in the middle of a live batch. At logon (the
# normal trigger for this script) there is no running server and therefore no
# fresh lock, so this does not affect the intended use case.
$envFile = Join-Path $projectRoot "private_controller\.env"
$dataDir = $null
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*#') { continue }
        if ($line -match "STRATEGY_DATA_DIR\s*=\s*(.+)$") { $dataDir = $Matches[1].Trim() }
    }
}
if (-not $dataDir) { $dataDir = Join-Path $projectRoot "data" }
$batchLock = Join-Path $dataDir "experiments\.batch.lock"
$staleLockSeconds = 15 * 60

if ((Test-Path $batchLock) -and (-not $Force)) {
    $lockAge = ((Get-Date) - (Get-Item $batchLock).LastWriteTime).TotalSeconds
    if ($lockAge -lt $staleLockSeconds) {
        Write-Host "Refusing to start: a batch lock is held and fresh (age $([math]::Round($lockAge))s < ${staleLockSeconds}s). A research batch is likely running. Re-run with -Force to override."
        exit 1
    }
}

# Kill any existing process on port 8078 before starting fresh
$existing = Get-NetTCPConnection -LocalPort 8078 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

Start-Process `
    -FilePath "py" `
    -ArgumentList "-3.12", "-m", "uvicorn", "strategy_lab.server:app", "--host", "127.0.0.1", "--port", "8078" `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError $errFile
