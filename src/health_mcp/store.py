"""Private SQLite persistence with explicit tenant keys and operational telemetry."""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("health_mcp.usage")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
                    account_hash TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (
                    owner TEXT NOT NULL, credentials TEXT NOT NULL, key TEXT NOT NULL,
                    payload TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL,
                    PRIMARY KEY(owner, credentials, key));
                CREATE TABLE IF NOT EXISTS cursors (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, credentials TEXT NOT NULL,
                    spec TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, credentials TEXT NOT NULL,
                    cursor TEXT, count INTEGER NOT NULL DEFAULT 0,
                    complete INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, user_id TEXT,
                    tool TEXT NOT NULL, outcome TEXT NOT NULL, error_code TEXT,
                    duration_ms REAL NOT NULL, google_requests INTEGER NOT NULL,
                    token_refreshes INTEGER NOT NULL, cache_hits INTEGER NOT NULL,
                    records INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS usage_time ON usage_events(at);
                CREATE INDEX IF NOT EXISTS usage_user_tool ON usage_events(user_id, tool);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_user(self, label: str, token: str | None = None) -> tuple[str, str]:
        token = token or secrets.token_urlsafe(48)
        uid = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO users(id,label,token_hash,created_at) VALUES(?,?,?,?)",
                (uid, label, digest(token), time.time()),
            )
        return uid, token

    def authenticate(self, token: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM users WHERE token_hash=? AND enabled=1",
                (digest(token),),
            ).fetchone()
        return row["id"] if row else None

    def bind_account(self, uid: str, account: str) -> bool:
        with self.connect() as db:
            db.execute(
                "UPDATE users SET account_hash=? WHERE id=? AND account_hash IS NULL",
                (digest(account), uid),
            )
            row = db.execute(
                "SELECT account_hash FROM users WHERE id=? AND enabled=1", (uid,)
            ).fetchone()
        return bool(row and row["account_hash"] == digest(account))

    def disable_user(self, label: str) -> int:
        with self.connect() as db:
            return db.execute("UPDATE users SET enabled=0 WHERE label=?", (label,)).rowcount

    def cache_get(self, owner: str, credentials: str, key: str) -> tuple[dict, float] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload,fetched_at FROM cache WHERE owner=? AND credentials=? AND key=? AND expires_at>?",
                (owner, credentials, key, time.time()),
            ).fetchone()
        return (json.loads(row["payload"]), row["fetched_at"]) if row else None

    def cache_put(self, owner: str, credentials: str, key: str, payload: dict, ttl: int) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)",
                (owner, credentials, key, json.dumps(payload), now, now + ttl),
            )

    def cursor_put(self, owner: str, credentials: str, spec: dict) -> str:
        cursor = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?,?)",
                (cursor, owner, credentials, json.dumps(spec), time.time() + 86400),
            )
        return cursor

    def cursor_get(self, owner: str, credentials: str, cursor: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT spec FROM cursors WHERE id=? AND owner=? AND credentials=? AND expires_at>?",
                (cursor, owner, credentials, time.time()),
            ).fetchone()
        return json.loads(row["spec"]) if row else None

    def export_create(self, owner: str, credentials: str, cursor: str | None) -> str:
        eid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO exports(id,owner,credentials,cursor,created_at) VALUES(?,?,?,?,?)",
                (eid, owner, credentials, cursor, time.time()),
            )
        return eid

    def export_get(self, owner: str, credentials: str, eid: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM exports WHERE id=? AND owner=? AND credentials=?",
                (eid, owner, credentials),
            ).fetchone()
        return dict(row) if row else None

    def event(self, user_id, tool, outcome, duration_ms, counters, error_code=None):
        event = dict(
            at=datetime.now(UTC).isoformat(),
            user_id=user_id,
            tool=tool,
            outcome=outcome,
            error_code=error_code,
            duration_ms=round(duration_ms, 2),
            google_requests=counters.google_requests,
            token_refreshes=counters.token_refreshes,
            cache_hits=counters.cache_hits,
            records=counters.records,
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO usage_events("
                + ",".join(event)
                + ") VALUES("
                + ",".join("?" for _ in event)
                + ")",
                tuple(event.values()),
            )
        logger.info(json.dumps(event, separators=(",", ":")))
        return event

    def stats(self, days: int = 30) -> dict:
        since = datetime.fromtimestamp(time.time() - days * 86400, UTC).isoformat()
        with self.connect() as db:

            def rows(sql):
                return [dict(r) for r in db.execute(sql, (since,))]

            return {
                "days": days,
                "by_tool": rows(
                    """SELECT tool, COUNT(*) calls, SUM(outcome!='ok') errors,
                              ROUND(AVG(duration_ms),1) avg_ms,
                              SUM(google_requests) google_requests,
                              SUM(cache_hits) cache_hits, SUM(records) records
                       FROM usage_events WHERE at>=?
                       GROUP BY tool ORDER BY calls DESC"""
                ),
                "by_day": rows(
                    """SELECT substr(at,1,10) day, COUNT(*) calls,
                              COUNT(DISTINCT user_id) active_users
                       FROM usage_events WHERE at>=?
                       GROUP BY day ORDER BY day"""
                ),
                "by_user": rows(
                    """SELECT user_id, COUNT(*) calls, MAX(at) last_call
                       FROM usage_events WHERE at>=? AND user_id IS NOT NULL
                       GROUP BY user_id ORDER BY calls DESC"""
                ),
                "errors": rows(
                    """SELECT error_code, COUNT(*) count FROM usage_events
                       WHERE at>=? AND error_code IS NOT NULL GROUP BY error_code"""
                ),
            }

    def prune(self, telemetry_days: int = 90) -> None:
        since = datetime.fromtimestamp(time.time() - telemetry_days * 86400, UTC).isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM cache WHERE expires_at<?", (time.time(),))
            db.execute("DELETE FROM cursors WHERE expires_at<?", (time.time(),))
            db.execute("DELETE FROM usage_events WHERE at<?", (since,))
