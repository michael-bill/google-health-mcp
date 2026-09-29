"""Google OAuth and read-only HTTP boundary; upstream secrets never enter errors."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

import httpx

from .errors import HealthError


@dataclass(repr=False)
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps([self.client_id, self.client_secret, self.refresh_token]).encode()
        ).hexdigest()


@dataclass
class Counters:
    google_requests: int = 0
    token_refreshes: int = 0
    cache_hits: int = 0
    records: int = 0


@dataclass(repr=False)
class Access:
    token: str
    expires_at: float
    scopes: list[str] = field(default_factory=list)


class GoogleClient:
    def __init__(self, http: httpx.AsyncClient | None = None, sleep=asyncio.sleep):
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        self.tokens: dict[str, Access] = {}
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.sleep = sleep

    async def close(self) -> None:
        await self.http.aclose()
        self.tokens.clear()

    async def access(self, cred: Credentials, counters: Counters, force: bool = False) -> Access:
        key = cred.fingerprint()
        async with self.locks[int(key[:8], 16) % len(self.locks)]:
            old = self.tokens.get(key)
            if old and old.expires_at > time.monotonic() + 60 and not force:
                return old
            if not all([cred.client_id, cred.client_secret, cred.refresh_token]):
                raise HealthError("GOOGLE_CREDENTIALS_MISSING")
            for attempt in range(3):
                counters.token_refreshes += 1
                try:
                    response = await self.http.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": cred.client_id,
                            "client_secret": cred.client_secret,
                            "refresh_token": cred.refresh_token,
                        },
                    )
                except httpx.HTTPError:
                    if attempt < 2:
                        await self.sleep(2**attempt)
                        continue
                    raise HealthError("GOOGLE_TOKEN_NETWORK_ERROR") from None
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    await self.sleep(2**attempt)
                    continue
                if response.status_code != 200:
                    self.tokens.pop(key, None)
                    raise HealthError(
                        "GOOGLE_REAUTHORIZE_REQUIRED"
                        if response.status_code in (400, 401)
                        else "GOOGLE_TOKEN_ERROR",
                        response.status_code,
                    )
                try:
                    data = response.json()
                    result = Access(
                        data["access_token"],
                        time.monotonic() + int(data.get("expires_in", 3600)),
                        data.get("scope", "").split(),
                    )
                except (ValueError, KeyError, TypeError):
                    raise HealthError("GOOGLE_TOKEN_RESPONSE_INVALID") from None
                if len(self.tokens) >= 256:
                    self.tokens.pop(next(iter(self.tokens)))
                self.tokens[key] = result
                return result
            raise HealthError("GOOGLE_TOKEN_ERROR")

    async def request(self, cred, counters, method, path, *, params=None, body=None, media=False):
        # Only this class constructs upstream URLs; callers cannot supply a host.
        if not path.startswith("users/me/") or ".." in path or "?" in path or "#" in path:
            raise HealthError("INVALID_UPSTREAM_PATH")
        if method not in ("GET", "POST") or (
            method == "POST" and not path.endswith((":rollUp", ":dailyRollUp"))
        ):
            raise HealthError("WRITE_OPERATION_FORBIDDEN")
        access = await self.access(cred, counters)
        refreshed = False
        for attempt in range(4):
            counters.google_requests += 1
            try:
                response = await self.http.request(
                    method,
                    "https://health.googleapis.com/v4/" + path,
                    params=params,
                    json=body,
                    headers={"Authorization": "Bearer " + access.token},
                )
            except httpx.HTTPError:
                if attempt < 3:
                    await self.sleep(2**attempt)
                    continue
                raise HealthError("GOOGLE_NETWORK_ERROR") from None
            if response.status_code == 401 and not refreshed:
                access = await self.access(cred, counters, force=True)
                refreshed = True
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                try:
                    delay = min(
                        30,
                        max(0, float(response.headers.get("Retry-After", 2**attempt))),
                    )
                except ValueError:
                    delay = 2**attempt
                await self.sleep(delay)
                continue
            if response.status_code != 200:
                codes = {
                    400: "GOOGLE_INVALID_QUERY",
                    401: "GOOGLE_REAUTHORIZE_REQUIRED",
                    403: "GOOGLE_PERMISSION_DENIED",
                    404: "GOOGLE_NOT_FOUND",
                    429: "GOOGLE_RATE_LIMITED",
                }
                raise HealthError(
                    codes.get(response.status_code, "GOOGLE_UPSTREAM_ERROR"),
                    response.status_code,
                )
            if media:
                return {"tcx": response.text}
            try:
                return response.json()
            except ValueError:
                raise HealthError("GOOGLE_RESPONSE_INVALID") from None
        raise HealthError("GOOGLE_RETRY_EXHAUSTED")
