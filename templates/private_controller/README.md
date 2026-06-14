# Private Controller Template

Use this template as the starting point for the separate private repository.

The private controller should own:

- Google Drive private storage
- Alpaca data download
- real experiment logs
- real run ledgers
- real reports
- candidate reviews
- private research decisions

The public worker repository should only receive public-safe requests and should
not own durable research memory.

