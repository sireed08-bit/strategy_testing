# Required Resources

## Available Now

- Python 3.10 or newer.
- Local JSONL experiment logs and research run ledgers.
- Synthetic data generation for plumbing checks.
- GitHub Actions workflows for tests and synthetic sharded research batches.

## Needed For Meaningful Research

- Historical daily OHLCV data with adjusted prices.
- A defined research universe, such as liquid ETFs, S&P 500 names, or sector
  ETFs.
- Stable symbol metadata so experiments can be compared over time.
- A policy for how many experiments to run per batch and how often.
- A decision about where durable research artifacts live:
  - local-only folder,
  - private cloud storage,
  - private GitHub artifact download,
  - or a future database.

## Optional Later

- Alpaca paper credentials stored only as local environment variables or GitHub
  Actions secrets.
- A self-hosted runner if public GitHub Actions artifacts are not acceptable.
- A private repository mirror for sensitive data, real reports, and long-lived
  experiment history.

## Current Capacity

- Local batch runner: configurable, default 20 experiments per run.
- GitHub Actions synthetic batch: 4 shards running concurrently, default 10
  experiments per shard.
- Experiment space: currently generates variations for moving-average crossover
  and RSI pullback strategies.
- Merge utility: combines shard outputs and deduplicates by experiment
  fingerprint.

The next capacity increase comes from adding more strategy implementations and
expanding `configs/experiment_space.yaml`.
