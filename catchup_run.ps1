# One-time catch-up run for the strategy lab. Mirrors the weekly auto-research
# pipeline (refresh data -> hill-climb around the best results -> prune -> sync).
# Invoked by one-off Windows scheduled tasks to make up for a missed week.
$ErrorActionPreference = "Continue"
$base = "http://127.0.0.1:8078"
$log = Join-Path $PSScriptRoot "logs\catchup.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Step($name, $url, $timeout) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        $r = Invoke-RestMethod $url -Method POST -TimeoutSec $timeout
        "$stamp  OK    $name" | Tee-Object -FilePath $log -Append
    } catch {
        # The auto-research call can exceed the client timeout while the server
        # keeps working; log it but continue the pipeline.
        "$stamp  WARN  $name -> $($_.Exception.Message)" | Tee-Object -FilePath $log -Append
    }
}

"=== catch-up run started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Tee-Object -FilePath $log -Append
Step "refresh-data"   "$base/refresh-data" 180
Step "auto-research"  "$base/auto-research?top_k=6&max_new_per_symbol=80" 1200
Step "prune"          "$base/prune" 300
Step "sync-private"   "$base/sync-private" 300
"=== catch-up run finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Tee-Object -FilePath $log -Append
