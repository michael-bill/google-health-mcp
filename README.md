# personal-health

Personal project for read-only access to Fitbit history through the Google Health API.

The planned MCP server will run locally or on an owner-controlled server. The server is not implemented yet; this repository currently contains the public project, privacy and terms pages required for OAuth configuration.

## Public website

Publish `docs/` from `main` with GitHub Pages. The expected project URL is:

https://michael-bill.github.io/google-health-mcp/

Pages:

- `docs/index.html` — project overview
- `docs/privacy.html` — privacy policy and intended data handling
- `docs/terms.html` — terms of use

The public website has no health API integration, secrets, analytics scripts or health records.

## Requested Google Health access

All requested scopes use the prefix `https://www.googleapis.com/auth/googlehealth.`:

- `activity_and_fitness.readonly`
- `health_metrics_and_measurements.readonly`
- `sleep.readonly`
- `profile.readonly`
- `settings.readonly`
- `nutrition.readonly`
- `location.readonly`

Keep OAuth client files, tokens and health exports outside tracked files and outside `docs/`. Never post them in issues or assistant messages.

The privacy policy must be reviewed and updated when actual storage, retention, hosting or connected-client behavior is implemented.
