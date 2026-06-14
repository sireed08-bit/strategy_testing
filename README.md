# Stock Strategy Development Lab

Research-focused system for discovering, testing, comparing, and improving stock
strategy ideas over time.

This project is not a live trading system. Its first job is to behave like a
disciplined research lab: propose experiments, run or record tests, score the
results, remember what was already tried, and produce clear next-step reports.

## Version 1 Boundary

- Research and development only.
- No live brokerage execution.
- Alpaca paper accounts may be used later for observation or simulated tracking.
- Prior trading systems can be used as references, but this project is not bound
  to their architecture, governance model, or strategy assumptions.

## Core Ideas

- Start with known strategy families: trend following, mean reversion, momentum,
  breakout, volatility, sector rotation, and risk-on/risk-off.
- Test variations in parameters, timeframes, entries, exits, sizing, and risk
  controls.
- Grade every result using a repeatable scoring model.
- Store every experiment with enough detail to understand what worked, what
  failed, and whether the idea should be revisited.
- Use evidence from prior tests to choose the next research direction.

## Repository Map

- [Architecture](docs/architecture.md) explains the intended system, data flow,
  research loop, scoring model, experiment memory, Alpaca paper role, phases, and
  v1 exclusions.
- [Research Criteria](configs/research_criteria.yaml) defines the default
  scoring thresholds and metric weights.
- [Experiment Log Schema](schemas/experiment_log.schema.json) describes the JSONL
  record shape for strategy experiments.
- [Research Run Schema](schemas/research_run.schema.json) describes the JSONL
  record shape for each research batch.
- [Required Resources](docs/resources.md) explains what is available now and what
  is still needed for meaningful research.
- [Public Repository Hardening](docs/public_repo_hardening.md) explains the
  public-repo privacy limits and guardrails.
- [Private/Public Split](docs/private_public_split.md) explains how the public
  worker and private controller should share work.
- `src/strategy_lab/` contains the initial reusable core for experiment specs,
  fingerprinting, scoring, experiment logs, run ledgers, reporting, and a small
  starter backtest engine.

## Quick Start

Install in editable mode:

```powershell
python -m pip install -e .
```

Run tests:

```powershell
python -m pytest
```

Create seed experiment records:

```powershell
python -m strategy_lab.cli seed --log data/experiments/experiment_log.jsonl
```

Run the first logged research batch:

```powershell
python -m strategy_lab.cli run-seed-batch --experiment-log data/experiments/experiment_log.jsonl --run-log data/runs/research_runs.jsonl --report reports/latest.md
```

Run a repeatable backtest batch that creates fresh strategy variations:

```powershell
python -m strategy_lab.cli run-backtest-batch --limit 20 --synthetic-days 756
```

Initialize a private Google Drive-backed storage folder:

```powershell
python -m strategy_lab.cli init-private-storage --root "G:\My Drive\Strategy Research Lab"
```

Download Alpaca IEX bars into private storage:

```powershell
python -m strategy_lab.cli download-alpaca-bars --symbols SPY,QQQ,IWM,DIA --start 2020-01-01 --end 2026-01-01 --output "G:\My Drive\Strategy Research Lab\data\market_data\alpaca_iex_etfs.csv"
```

Create a public-safe batch request:

```powershell
python -m strategy_lab.cli write-batch-request --output "G:\My Drive\Strategy Research Lab\outbox\public_worker_requests\batch_request.json" --purpose "Public-safe synthetic validation" --strategy-names moving_average_cross,rsi_pullback --symbols SPY,QQQ --dataset-name synthetic_public_safe --max-experiments 40 --shard-count 4
```

Encrypt any bundle that may cross public infrastructure:

```powershell
$env:STRATEGY_BUNDLE_PASSPHRASE="use-a-long-private-passphrase"
python -m strategy_lab.cli encrypt-file --input "G:\My Drive\Strategy Research Lab\outbox\public_worker_requests\bundle.zip" --output "G:\My Drive\Strategy Research Lab\outbox\public_worker_requests\bundle.zip.encrypted"
```

Score a metrics payload:

```powershell
python -m strategy_lab.cli score --metrics examples/sample_metrics.json
```

Run the built-in sample backtest:

```powershell
python -m strategy_lab.cli backtest-sample
```

Write a Markdown research report from an experiment log:

```powershell
python -m strategy_lab.cli report --log data/experiments/experiment_log.jsonl --output reports/latest.md
```

## Next Build Step

The next practical build step is adding real historical data and more strategy
implementations. The project now has a repeatable batch runner that can generate
fresh strategy variations, skip duplicates, write experiment records, append one
research run record, and produce a report. GitHub Actions can run synthetic
research batches across four concurrent shards.

For private work, use the public-safe `templates/private_controller/` scaffold
as the starting point for a separate private repository. This local workspace
also has an ignored `private_controller/` folder for your private-side setup.
