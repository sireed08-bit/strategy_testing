# Strategy Lab Server — auto-start script
# Registered with Windows Task Scheduler to run at user logon.
# Starts uvicorn as a hidden background process; logs go to logs\server.log

$projectRoot = "C:\Users\sir01\OneDrive\Documents\GitHub\strategy_testing"
$logDir = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$logFile  = Join-Path $logDir "server.log"
$errFile  = Join-Path $logDir "server_error.log"

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
