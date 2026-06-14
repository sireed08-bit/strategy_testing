# GitHub Deployment Checklist

Target repository: `sireed08-bit/strategy_testing`

## Current Readiness

The project is ready for a first public GitHub deployment after a final human
review of staged files. The repository should be treated as public source code
only, not as a store for private research history.

## What Should Be Pushed

- Source code under `src/strategy_lab/`
- Tests under `tests/`
- Configuration templates under `configs/`
- JSON schemas under `schemas/`
- Documentation under `docs/`
- GitHub Actions workflows under `.github/workflows/`
- Empty folder placeholders such as `.gitkeep`

## What Should Not Be Pushed

- Real market data
- Vendor datasets
- Alpaca credentials
- `.env` files
- Long-lived experiment logs with meaningful private conclusions
- Generated reports from real research runs
- Account identifiers or broker responses

## Public Exposure Warning

Public GitHub repositories are visible to indexers and bots. `.gitignore`,
short artifact retention, and read-only workflows reduce accidental exposure,
but they do not make a public repository private. Use a private repository or
private storage for sensitive data and real long-term research records.

## Deployment Commands

Only run these after reviewing `git status --short` and staged files:

```powershell
git add .github .gitignore .env.example README.md pyproject.toml configs docs examples schemas src tests data/experiments/.gitkeep data/runs/.gitkeep reports/.gitkeep
git commit -m "initial strategy research lab scaffold"
git push -u origin main
```

## Post-Deployment

1. Confirm the CI workflow passes.
2. Trigger the synthetic research workflow manually.
3. Confirm the workflow artifacts contain only synthetic records.
4. Keep real research logs local or in private storage.

