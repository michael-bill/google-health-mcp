"""OAuth persistence: encrypted Google credentials and hashed MCP grants."""

import json
import secrets
import time
import uuid

from .errors import HealthError
from .google import Credentials
from .store import Store, digest
from .vault import TokenVault


class OAuthStore:
    def __init__(self, store: Store, vault: TokenVault, client_id: str, client_secret: str):
        self.store, self.vault = store, vault
        self.client_id, self.client_secret = client_id, client_secret
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS google_connections (
                    owner TEXT PRIMARY KEY, account_hash TEXT NOT NULL UNIQUE,
                    client_id TEXT NOT NULL, encrypted_refresh TEXT NOT NULL,
                    scopes TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_pending (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
                    expires_at REAL NOT NULL, csrf_hash TEXT, browser_hash TEXT,
                    google_state_hash TEXT UNIQUE, encrypted_verifier TEXT, invite_owner TEXT);
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    hash TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
                    client_id TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL,
                    family TEXT NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS oauth_grant_family ON oauth_grants(family);
            """)
        self.prune()

    def prune(self):
        now = time.time()
        with self.store.connect() as db:
            db.execute("DELETE FROM oauth_pending WHERE expires_at<?", (now,))
            db.execute("DELETE FROM oauth_codes WHERE expires_at<?", (now,))
            # Keep spent refresh tokens until expiry to detect replay and revoke a family.
            db.execute("DELETE FROM oauth_grants WHERE expires_at<?", (now,))

    def save_connection(
        self, account: str, refresh: str | None, scopes: list[str], invite_owner=None
    ):
        account_hash = digest(account)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT id FROM users WHERE account_hash=? AND enabled=1", (account_hash,)
            ).fetchone()
            if invite_owner:
                invited = db.execute(
                    "SELECT id,account_hash FROM users WHERE id=? AND enabled=1", (invite_owner,)
                ).fetchone()
                if not invited or invited["account_hash"] not in (None, account_hash):
                    raise HealthError("GOOGLE_ACCOUNT_MISMATCH")
                if owner and owner["id"] != invite_owner:
                    raise HealthError("GOOGLE_ACCOUNT_ALREADY_LINKED")
                owner = invited
            if not owner:
                raise HealthError("INVITATION_REQUIRED")
            uid = owner["id"]
            old = db.execute("SELECT * FROM google_connections WHERE owner=?", (uid,)).fetchone()
            context = f"google:{uid}:{self.client_id}"
            if refresh:
                encrypted = self.vault.seal(refresh, context)
            elif old and old["client_id"] == self.client_id:
                encrypted = old["encrypted_refresh"]
            else:
                raise HealthError("GOOGLE_REFRESH_TOKEN_MISSING_RECONNECT")
            db.execute("UPDATE users SET account_hash=? WHERE id=?", (account_hash, uid))
            db.execute(
                "INSERT OR REPLACE INTO google_connections VALUES(?,?,?,?,?,?)",
                (uid, account_hash, self.client_id, encrypted, json.dumps(scopes), time.time()),
            )
        return uid

    def credentials(self, uid: str) -> Credentials:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT c.* FROM google_connections c JOIN users u ON u.id=c.owner "
                "WHERE c.owner=? AND u.enabled=1",
                (uid,),
            ).fetchone()
        if not row or row["client_id"] != self.client_id:
            raise HealthError("GOOGLE_CONNECTION_REQUIRED")
        refresh = self.vault.open(row["encrypted_refresh"], f"google:{uid}:{self.client_id}")
        return Credentials(self.client_id, self.client_secret, refresh)

    def revoke_user(self, uid: str):
        with self.store.connect() as db:
            db.execute("UPDATE oauth_grants SET revoked=1 WHERE owner=?", (uid,))
            db.execute("DELETE FROM google_connections WHERE owner=?", (uid,))

    def grant(self, token: str, kind: str, include_revoked=False):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT g.* FROM oauth_grants g JOIN users u ON u.id=g.owner "
                "JOIN google_connections c ON c.owner=g.owner "
                "WHERE g.hash=? AND g.kind=? AND g.expires_at>? AND u.enabled=1",
                (digest(token), kind, time.time()),
            ).fetchone()
        if not row or (row["revoked"] and not include_revoked):
            return None
        return dict(row)

    def issue(self, db, uid: str, client_id: str, scopes: list[str], resource: str, family=None):
        family = family or uuid.uuid4().hex
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        for value, kind, lifetime in ((access, "access", 900), (refresh, "refresh", 30 * 86400)):
            db.execute(
                "INSERT INTO oauth_grants VALUES(?,?,?,?,?,?,?,?,0)",
                (
                    digest(value),
                    kind,
                    uid,
                    client_id,
                    json.dumps(scopes),
                    resource,
                    family,
                    time.time() + lifetime,
                ),
            )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": 900,
            "scope": " ".join(scopes),
        }
