# Deployment

Use one worker/process with persistent private storage. GitHub Pages serves only
the documentation; it cannot run the backend.

1. Install the package or build the Dockerfile.
2. Mount the Google OAuth JSON read-only and configure `.env`.
3. Set `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` to the real host/HTTPS origin.
4. Bind the service to loopback behind a TLS reverse proxy (Caddy example included).
5. Create individual users against the same SQLite database and distribute keys
   privately. Google refresh tokens belong in each client's environment/file.

From the repository root:

```sh
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

The container runs as UID 10001. Ensure mounted credentials are readable by this
UID without making them world-readable. The host port is loopback-only. Replace
the example hostname and configure DNS before enabling HTTPS.

`/healthz` tests liveness, not Google connectivity. Use CLI stats and sanitized
JSON logs for monitoring. Schedule `prune`, rotate logs and use SQLite's backup
API for consistent backups during WAL writes. Backups contain private health data.

Deployment files are templates; the project does not automatically install an
OS service, provision DNS/TLS or deploy to a remote machine.
