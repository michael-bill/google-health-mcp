# Google Health MCP

Private, read-only Fitbit access through Google Health API. Python 3.11+, SQLite,
Streamable HTTP and stdio. Each user supplies their own Google authorization.

Two explicit authentication modes are supported. `AUTH_MODE=forwarded` keeps the
original per-request Google credentials and local stdio workflow. `AUTH_MODE=oauth`
uses browser login, independent MCP tokens and encrypted server-side Google
connections. There is no automatic fallback between modes.

## Features

- 37 catalog data types: activity, health measurements, sleep and nutrition.
- Profile, settings, paired devices and explicit TCX workout/GPS export.
- Raw and reconciled records, individual records, daily and physical-time rollups.
- User-bound pagination, aggregation chunking and resumable JSONL exports.
- SQLite usage analytics and sanitized JSON logs.
- No Google writes, no tokens in tool arguments/results, no clinical diagnoses.

Coverage means API support, not that every account/device has every measurement.
The catalog is based on https://developers.google.com/health/data-types.

## Install and run locally (forwarded mode)

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
chmod 600 .env
```

Set `GOOGLE_CLIENT_SECRETS_FILE` in the private `.env` to the downloaded Google
OAuth client JSON. Obtain a refresh token using your own OAuth client and the
seven scopes below. Never paste credentials into an assistant or public issue.

```sh
google-health-mcp add-user owner --credentials-file .secrets/client.env
google-health-mcp serve --transport http
```

`add-user` generates a personal MCP key, writes it directly to a mode-0600 file,
and stores only its hash in SQLite. Add `GOOGLE_REFRESH_TOKEN` to that client file
or supply it through the client process environment.

The endpoint is `http://127.0.0.1:8767/mcp`. `/healthz` is a public liveness check.
Use HTTPS for every non-loopback connection. Relative file paths resolve from
the server working directory; launch from the repository root.

## Connect clients in forwarded mode

| Header | Purpose |
| --- | --- |
| `Authorization: Bearer …` | Personal MCP key |
| `X-Google-Refresh-Token` | User's Google refresh token |
| `X-Google-Client-Id`, `X-Google-Client-Secret` | Optional pair for a separate OAuth app |

Without the optional pair, the configured server OAuth client is used. Refresh
tokens must belong to that client. This mode uses manually provisioned credentials.

For Codex, adapt `examples/codex-http.toml`. The private `http_headers_helper`
loads credentials outside tool arguments. **Do not run the `headers` command in
an agent-visible terminal:** its stdout intentionally carries credentials through
the MCP client's private subprocess pipe. `examples/codex-env.toml` shows env
header injection. A `.env` file does not populate Codex's process environment.

Clients without configurable headers can use the OAuth mode below. Alternatively
run one local copy per user:

```sh
google-health-mcp serve --transport stdio --credentials-file .secrets/client.env
```

## Browser login (OAuth mode)

Use an HTTPS origin, register `<origin>/oauth/google/callback` on the Google web
OAuth client, and configure:

```dotenv
AUTH_MODE=oauth
PUBLIC_BASE_URL=https://health.example.com
TOKEN_KEY_FILE=/private/path/token-keys.json
```

Create the key file once with `google-health-mcp init-vault --key-file ...`.
Never regenerate it over an existing deployment. Keep an encrypted backup of the
key ring separate from database backups. Google client credentials remain in the
server's private OAuth JSON file.

Point the MCP client at `<origin>/mcp` without Google headers. For Codex, see
`examples/codex-oauth.toml`, or use
`codex mcp add google_health_remote --url <origin>/mcp --oauth-client-registration dcr`.
To reconnect, use `codex mcp login google_health_remote --oauth-client-registration dcr`.
The server supports discovery,
dynamic client registration, authorization code + S256 PKCE, token refresh and
revocation. It does not fetch Client ID Metadata Documents.

The server displays explicit consent identifying the client and its callback
before sending the browser to Google. For first-time enrollment, the operator
runs `add-user` and privately gives that user the generated invitation key.
In OAuth mode this key is accepted only during enrollment, never as an HTTP bearer
credential. First enrollment binds it to the verified Google account. Later
logins use that account and can leave the invitation field blank.

Google refresh tokens are AES-GCM encrypted in SQLite, bound to their user and
OAuth client. MCP access tokens last 15 minutes; MCP refresh tokens last 30 days
from issuance and rotate on use. Replaying a spent refresh token revokes its
entire token family. Only hashes of MCP tokens and authorization codes are stored.

`disconnect-google <label>` deletes the encrypted Google connection and revokes
MCP sessions, but does not revoke consent at Google or delete cached health records
and exports. `revoke-user <label>` disables all further access for the user in both
modes. New browser authorization may be needed when Google revokes or expires its
refresh token.

For migration, `import-google <label> --credentials-stdin` accepts a JSON object
with a `refresh_token` through a **private pipe**, verifies Google identity and
stores the encrypted connection. Never pass the token as a command-line argument
or print that input in an agent-visible terminal.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_data_types` | Catalog, supported read operations and scopes |
| `get_data_status` | Verify access and inspect granted scopes |
| `get_profile`, `get_settings`, `get_devices` | Account and synchronization context |
| `query_data` | Query any supported type |
| `next_page` | Continue a query using its opaque cursor |
| `get_record` | Fetch one complete record where supported |
| `get_sleep`, `get_workouts` | Convenient specialized queries |
| `get_health_summary` | Bounded overview with per-metric continuation cursors |
| `export_data` | Start/resume a private JSONL export |
| `export_workout_route` | Explicit TCX export, potentially containing GPS |
| `read_export` | Read a private export in bounded text chunks |

Ranges are **start-inclusive, end-exclusive**. Date-only inputs use recorded civil
time. Sleep is selected by END time. Daily metrics require dates; physical rollups
require timezone-aware timestamps. Food catalogs have no time filter. Profile
and settings are current snapshots, not historical versions.

`query_data` modes: `auto`, `list`, `reconcile`, `dailyRollup`, `rollup`. Auto prefers
reconcile to avoid overlapping device records. Daily rollups use one-day windows;
physical rollups accept `window_seconds`. Large rollup ranges are chunked within
Google's 14/90-day limits while preserving aggregation window boundaries.

**Empty pages may still have `next_cursor`.** Continue until `complete=true`.
Missing values never become zeros. Page sizes are bounded to 100 (25 for sleep
and exercise). Cursors expire after 24 hours and are bound to the user and their
Google credentials. Reauthorization may require restarting a query.

Exports run in bounded batches: call again with `export_id` until complete,
within 24 hours between batches. `/exports/<id>` requires the same user's MCP
key. Download outside model context, or explicitly use `read_export` to return
bounded chunks to the agent. Files remain private on the server until
the operator removes them; no anonymous download URLs are generated.

## Multiple users

```sh
google-health-mcp add-user friend --credentials-file .secrets/friend.env
google-health-mcp revoke-user friend
```

Deliver the key privately. In forwarded mode each friend keeps their Google token
on their own computer; in OAuth mode they use the key to enroll through browser
login. HTTP requests never inherit the owner's refresh token. First access
binds a user to the verified Google Health identity. Caches, cursors and exports
are isolated by owner. Switching Google accounts requires a new MCP user.

## Statistics

```sh
google-health-mcp stats --days 30
google-health-mcp prune
```

Reports include calls/errors/average latency by tool, daily activity, active users,
per-user counts, upstream requests, cache hits and returned-record counts.
Global statistics are available only through the operator CLI.

Usage events contain UTC time, opaque user ID, tool name, outcome, duration,
fixed error code and counters. The same events are logged as JSON on stderr.
No tool arguments, health values, date ranges, tokens or IP addresses are logged.
Tool names themselves reveal the requested category, so restrict log access.
SDK schema validation failures before handler execution are not handler calls;
transport authentication failures have separate events.

Cache validity defaults to five minutes. Expired cache/cursors and usage older
than 90 days are pruned on startup or `prune`; the systemd deployment includes a
daily timer. Expired OAuth transactions and grants are pruned as well.
TTL invalidates cached results but does not guarantee secure disk erasure.

## Development

```sh
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
google-health-mcp smoke
python scripts/verify_live.py
python scripts/verify_flow.py
```

Tests are offline with fake credentials. The final three commands explicitly use
real credentials but print only statuses/counts. Live MCP verification requires
a running HTTP server. MCP SDK is pinned to its maintained 1.x API; upgrade major
versions deliberately and re-run transport integration tests.

See `CONTRIBUTING.md`, `SECURITY.md`, and `deploy/README.md`.

## Google scopes

All use the prefix `https://www.googleapis.com/auth/googlehealth.`:

```text
activity_and_fitness.readonly
health_metrics_and_measurements.readonly
sleep.readonly
profile.readonly
settings.readonly
nutrition.readonly
location.readonly
```

The documentation/privacy website is published from `docs/` at
https://michael-bill.github.io/google-health-mcp/.
