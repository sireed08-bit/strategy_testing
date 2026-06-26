# Migration Checklist

## Public Repository

- URL: `https://github.com/sireed08-bit/strategy_testing`
- Visibility: PUBLIC
- Purpose: reusable code, tests, schemas, docs, examples, templates, and
  public-safe GitHub Actions workflows.

Clone and set up on the new computer:

```powershell
git clone https://github.com/sireed08-bit/strategy_testing.git
Set-Location strategy_testing
python -m pip install -e ".[dev]"
```

Run tests:

```powershell
python -m pytest
```

Run API-free smoke checks:

```powershell
python -m strategy_lab.cli score --metrics examples/sample_metrics.json
python -m strategy_lab.cli backtest-sample
$tmp = Join-Path $env:TEMP "strategy_lab_smoke"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
python -m strategy_lab.cli run-backtest-batch --experiment-log (Join-Path $tmp "experiment_log.jsonl") --run-log (Join-Path $tmp "research_runs.jsonl") --report (Join-Path $tmp "latest.md") --limit 3 --synthetic-days 260 --purpose "New computer smoke test"
```

## Ignored Private Files

These files are intentionally ignored by the public repo:

- `data/experiments/experiment_log.jsonl`
- `data/runs/research_runs.jsonl`
- `reports/latest.md`
- `private_controller/`

They are migration state, not public source code. Restore them from the private
state repo after cloning the public repo.

## Private State Repository

- URL: `https://github.com/sireed08-bit/strategy_testing_private_state`
- Visibility: PRIVATE
- Purpose: migration-needed private state only.

Clone and restore:

```powershell
git clone https://github.com/sireed08-bit/strategy_testing_private_state.git
Copy-Item -Recurse -Force strategy_testing_private_state\data strategy_testing\
Copy-Item -Recurse -Force strategy_testing_private_state\reports strategy_testing\
Copy-Item -Recurse -Force strategy_testing_private_state\private_controller strategy_testing\
```

Expected restored files:

- `strategy_testing\data\experiments\experiment_log.jsonl`
- `strategy_testing\data\runs\research_runs.jsonl`
- `strategy_testing\reports\latest.md`
- `strategy_testing\private_controller\README.md`
- `strategy_testing\private_controller\private_config.example.yaml`
- `strategy_testing\private_controller\.gitignore`

## Private Storage Recreation

Create or select a private Google Drive folder, then initialize the storage
layout:

```powershell
python -m strategy_lab.cli init-private-storage --root "G:\My Drive\Strategy Research Lab"
```

Set local environment variables only on the private machine:

```powershell
$env:STRATEGY_PRIVATE_STORAGE_ROOT="G:\My Drive\Strategy Research Lab"
$env:ALPACA_PAPER_API_KEY="your_key_here"
$env:ALPACA_PAPER_API_SECRET="your_secret_here"
$env:STRATEGY_BUNDLE_PASSPHRASE="use-a-long-private-passphrase"
```

Do not commit real `.env` files, API keys, broker credentials, cookies, private
market data, or account identifiers to either repository.

## Visibility Warning

The public repository is visible to indexers and bots. Keep the public repo for
generic code and synthetic or sanitized examples only. Keep real state, durable
logs, and meaningful research reports in the private repo or private storage.

