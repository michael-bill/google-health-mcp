# Credential and data boundaries

## Authentication modes

- `forwarded`: manually provisioned MCP keys identify users; only hashes are
  stored. Users forward Google credentials on each request. The server does not
  persist those credentials. This mode also supports local stdio.
- `oauth`: browser consent and Google OAuth establish server-side connections.
  Google refresh tokens and registered OAuth client secrets are AES-GCM encrypted
  in SQLite. The versioned key ring must be kept outside the database and Git.
  Associated data binds ciphertext to its user/client or transaction purpose.
  MCP grants use independent opaque tokens; only hashes are persisted. Access
  tokens expire after 15 minutes. Refresh tokens rotate and expire after 30 days;
  reuse of a spent refresh token revokes its family.
- Modes are explicit: OAuth HTTP never accepts legacy keys or Google headers as
  an alternative authentication mechanism. Forwarded mode does not expose OAuth
  endpoints. Dynamic client registration is not user registration: a user still
  needs an invitation or an already linked Google account.

## OAuth controls

The SDK validates client authentication, redirect URI equality, scopes and S256
PKCE. Our provider binds the audience to the configured MCP resource; enforces
atomic single-use authorization codes; checks Google identity; and separates MCP
tokens from Google credentials. Google callback state is one-time, expires after
ten minutes and is bound to an HttpOnly/Secure browser cookie. A same-origin,
CSRF-protected explicit consent step precedes the Google redirect. The callback
never logs tokens or codes. Client ID Metadata Documents are not fetched; DCR
and stored clients are supported. Public OAuth endpoints have bounded bodies,
registration capacity and in-process rate limiting.

## Operational limits

- Use TLS for non-loopback connections. Disable proxy access logs containing
  callback query strings, and never dump headers or token endpoint bodies.
- A Google identity check precedes health tools, including cached reads. Tool
  arguments cannot switch users, override upstream hosts or perform Google writes.
- Google access tokens exist only in process memory. The running application and
  its machine administrator can access decrypted credentials. Encryption protects
  a database-only leak, not full server compromise.
- Health cache and export files are **not encrypted by the application**. Use
  encrypted disks, private backups and restrictive filesystem permissions.
- Files use private permissions. This does not isolate processes under the same
  OS account. The systemd deployment uses a dedicated unprivileged account.
- Downloading an export requires its owner's current MCP bearer credential, but
  does not re-check Google consent. Revoking Google consent alone does not delete
  prior exports. In OAuth mode disconnecting the server connection revokes MCP
  grants; in either mode disabling the user blocks access. Delete stored copies
  separately when requested.
- Run one worker with SQLite; export locks and rate limits are in-process. The
  provided systemd unit sets a memory limit and a daily retention timer.
- Back up the encryption key ring separately. Losing it requires reconnecting
  users. Preserve old key versions until all corresponding ciphertext is migrated.

Do not post health records or credentials in public issues. Reproduce problems
using synthetic fixtures and coordinate sensitive security details privately.
