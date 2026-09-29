# Contributing

Install `pip install -e '.[test]'` in a Python 3.11+ virtual environment. Run Ruff
and pytest before commits. Live API checks belong in opt-in `scripts/`, not CI.

## Code layout

- `catalog.py` / `catalog.json`: supported types, operations and time filters.
- `google.py`: token exchange, upstream HTTP, retry policy and sanitized errors.
- `service.py`: pagination, aggregation chunking, summaries and export workflows.
- `store.py`: users, cache, cursors, exports and operational statistics in SQLite.
- `server.py`: MCP contracts, request identity and HTTP authentication.
- `cli.py`: startup, local administration and client header helper.
- `config.py`: environment and private OAuth configuration.

Keep handlers thin and business logic independently testable. Preserve units and
source fields. Never infer zero from missing data, discard a pagination cursor,
or log credential-bearing exceptions. Cover cross-user isolation and partial
responses with synthetic tests. Update privacy documentation when storage or
sharing changes. Never commit private `.env`, SQLite or real account fixtures.

Use a short meaningful commit title without a body.
