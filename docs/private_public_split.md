# Private/Public Split

## Public Worker Repository

The public repository is for generic code, synthetic tests, and public-safe
compute. It should not own the real research memory.

Allowed in public:

- source code
- tests
- synthetic workflows
- schemas
- public-safe batch request shape
- generic strategy families

Not allowed in public:

- Alpaca credentials
- real market data
- real experiment logs
- real run ledgers
- real reports
- private strategy conclusions
- private symbol universes

## Private Controller Repository

The private controller owns the real research process:

- Google Drive private storage root
- Alpaca data download
- durable experiment logs
- durable run ledgers
- final reports
- candidate reviews
- private batch decisions

This repository includes a public-safe `templates/private_controller/` scaffold.
This local workspace also includes an ignored `private_controller/` folder that
can become the separate private GitHub repository.

## Handoff Pattern

```text
Private controller
  -> writes sanitized batch_request.json
  -> triggers or uploads to public worker

Public worker
  -> runs sharded public-safe compute
  -> returns synthetic or sanitized bundle

Private controller
  -> imports result bundle
  -> writes real logs/reports to Google Drive
```

If a result bundle contains meaningful private research conclusions, encrypt it
before it leaves the private environment:

```powershell
$env:STRATEGY_BUNDLE_PASSPHRASE="use-a-long-private-passphrase"
python -m strategy_lab.cli encrypt-file --input result.bundle.zip --output result.bundle.zip.encrypted
```

Keep the passphrase out of the public repository and out of public workflow logs.
