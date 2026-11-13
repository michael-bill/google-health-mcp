# Deployment

The primary server deployment uses Python 3.11+, systemd and an existing nginx.
Use one process and persistent private SQLite storage. Docker remains optional
for the original forwarded mode; no container is required.

## systemd + nginx

1. Create a dedicated `health-mcp` system user without shell access.
2. Install the source at `/opt/google-health-mcp` and create `.venv` there.
   Install with `.venv/bin/pip install --no-cache-dir .`.
3. Create `/etc/google-health-mcp` as root:health-mcp, mode 0750. Put the private
   Google client JSON there, readable by the service group only. Copy and edit
   `server.env.example` as `server.env` in that directory.
4. Run `google-health-mcp init-vault --key-file /etc/google-health-mcp/token-keys.json`
   once; make the key file root:health-mcp, mode 0640. Back it up separately.
5. Create `/var/lib/google-health-mcp`, owned by health-mcp, mode 0700. Use
   `--env-file /etc/google-health-mcp/server.env` for operator CLI commands.
6. Create users with `add-user <label> --credentials-file <private-path>` and
   deliver invitation keys privately. They are used on first browser connection.
7. Point a hostname at the server. Add its HTTPS `/oauth/google/callback` to the
   Google OAuth web client, preserving existing callbacks.
8. Obtain a certificate with Certbot using an HTTP webroot virtual host first.
   Replace `health.example.com` in `nginx.conf.example` and install it as a new,
   dedicated nginx configuration. Run `nginx -t` before reloading. Configure a
   certificate renewal deploy hook to validate and reload nginx.
9. Install the three `google-health-mcp*.service/timer` files into
   `/etc/systemd/system/`, then enable the service and retention timer:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now google-health-mcp.service google-health-mcp-prune.timer
```

The application binds only to loopback. nginx provides HTTPS. The service has
restricted filesystem access, a 512 MiB memory ceiling and no Linux capabilities.
No existing application, container or virtual host needs to be removed or stopped.

## Operations

```sh
sudo systemctl status google-health-mcp
sudo journalctl -u google-health-mcp --since today
sudo -u health-mcp /opt/google-health-mcp/.venv/bin/google-health-mcp --env-file /etc/google-health-mcp/server.env stats --days 30
```

The daily timer removes expired cache/cursors and events older than 90 days.
Exports are retained until explicitly removed. Journal retention follows the
host's configuration. Use SQLite's backup API for consistent WAL-mode backups.
Health data in backups is private even though Google refresh tokens are encrypted.

For updates, test first, take a private SQLite backup, install the new package in
the same venv, and restart **only** `google-health-mcp`. Preserve data, secrets and
the key ring. Roll back the package if the health check fails. Do not regenerate
the vault key, run broad Docker cleanup, or modify other nginx sites.

`/healthz` checks liveness. OAuth discovery lives at
`/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource/mcp`. Real Google connectivity requires an
authenticated MCP call.

## Optional Docker / forwarded mode

The original Dockerfile and `compose.yaml` still support `AUTH_MODE=forwarded`.
From the repository root:

```sh
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

The container uses UID 10001 and a loopback host port. Mount credentials read-only
with suitable ownership. Put TLS in front of every non-loopback deployment.
OAuth in Docker additionally requires a persistent key-ring mount and its settings;
the provided Compose example does not provision these automatically.

## Git-based Docker Swarm deployment

For a single-node Swarm behind an independent Caddy, clone the repository with a
read-only deploy key into `/opt/google-health-mcp`. Copy `deploy/swarm.env.example`
to `.env` and set `HEALTH_DOMAIN`. Install the existing web OAuth JSON and a newly
created vault key as `google-client.json` and `token-keys.json` under
`/etc/google-health-mcp` (directory mode 0700, owned by UID 10001; files 0400).
Create `/var/lib/google-health-mcp` owned by UID 10001, mode 0700. These directories
remain outside the checkout and are mounted into the container.

Run `./deploy.sh` (or `./deploy.sh branch`). It fetches and fast-forwards the chosen
branch, builds an image tagged with the full Git commit, and updates only the
`google-health` stack. SQLite requires a single replica. The service publishes no
host ports and has a 512 MiB memory limit and a 0.5 CPU limit.

Attach the independent Caddy service to the external overlay `health-backend` and
proxy the HTTPS hostname to `google-health_mcp:8767`. Manage that Caddy in its own
stack; deploying this application never updates Caddy or Things Plus.

For an existing installation, preserve the vault key and SQLite database before
updates. The deployment script does not delete data or exports.
