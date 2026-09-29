# Credential and data boundaries

- Use TLS for non-loopback connections and keep access limited to known users.
- MCP keys identify users; only their hashes are stored in SQLite.
- Google credentials are forwarded to the server. They are hidden from model
  tool arguments, but accessible to the server process/operator.
- A Google identity check precedes health tools, including cached reads. First
  use binds a verified Google identity to a user. Tool arguments cannot switch
  users, override upstream hosts or perform Google writes.
- Refresh tokens/client secrets are not stored in SQLite. Access tokens exist
  only in process memory. Upstream errors are converted to fixed safe codes.
- Cache and export files contain health data and are **not encrypted at rest**
  by the application. Use encrypted disks, private backups and restrictive ACLs.
- Private files use 0600 and directories 0700 when created. This does not isolate
  processes under the same OS user. An agent with unrestricted filesystem access
  can still read them; use OS isolation for a stronger guarantee.
- Disable request/header dumps in proxies, tracers and error reporters. Do not
  pass secrets in URLs. The header helper is for the MCP client, not a tool call.
- Exports remain accessible with the owner's MCP key until removed or the MCP
  key is revoked, independently of subsequent Google consent revocation.
- Run one worker with SQLite. Export locks are in-process. Configure reverse
  proxy admission/rate limits before opening a larger deployment.

Do not post health records or credentials in public issues. Reproduce bugs using
synthetic fixtures and coordinate sensitive security details privately.
