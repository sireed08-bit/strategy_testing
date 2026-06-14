# Public Repository Hardening

This repository is intended to be public, so it must not contain secrets, private
market data, proprietary research results, or real broker artifacts.

## Important Limitation

A public GitHub repository cannot be made truly private from search engines,
indexers, data mirrors, or bots. The only reliable privacy control is a private
repository or private storage. The hardening steps here reduce accidental
exposure; they do not guarantee secrecy.

## Rules For This Repository

- Do not commit Alpaca credentials, `.env` files, API keys, tokens, or account
  identifiers.
- Do not commit real market data, vendor downloads, or paid datasets.
- Do not commit long-lived research outputs if they reveal proprietary strategy
  conclusions.
- Keep generated experiment logs and reports local by default.
- Use GitHub Actions synthetic runs only for workflow validation.
- Store real research artifacts outside this public repository unless they are
  intentionally sanitized.

## Guardrails Already Added

- `.gitignore` excludes secrets, local data, JSONL experiment logs, run ledgers,
  generated reports, and action artifacts.
- `.env.example` documents credential names without real values.
- GitHub Actions workflows use read-only repository permissions.
- The synthetic research workflow has a one-day artifact retention period.
- No workflow uses broker secrets or real market data.

## Recommended Deployment Practice

Before pushing, run:

```powershell
python -m pytest
git status --short
git diff --check
```

Review every file staged for commit. If real data or private conclusions appear,
remove them before pushing.

