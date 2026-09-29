import asyncio

import httpx
import pytest

from health_mcp.errors import HealthError


async def test_refresh_is_single_flight(google, caller, fake):
    await asyncio.gather(*(google.access(caller.credentials, caller.counters) for _ in range(10)))
    assert len(fake.requests) == 1
    assert caller.counters.token_refreshes == 1


async def test_401_refreshes_once(google, caller, fake):
    attempts = 0

    def handler(req):
        nonlocal attempts
        attempts += 1
        return httpx.Response(401 if attempts == 1 else 200, json={})

    fake.handler = handler
    await google.request(caller.credentials, caller.counters, "GET", "users/me/profile")
    assert caller.counters.token_refreshes == 2
    assert caller.counters.google_requests == 2


async def test_429_retries_and_never_exposes_error_body(google, caller, fake):
    fake.handler = lambda req: httpx.Response(
        429, json={"error": "sensitive secret and medical record"}
    )
    with pytest.raises(HealthError) as raised:
        await google.request(caller.credentials, caller.counters, "GET", "users/me/profile")
    assert str(raised.value) == "GOOGLE_RATE_LIMITED"
    assert caller.counters.google_requests == 4


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "users/me/profile"),
        ("POST", "users/me/profile"),
        ("GET", "https://evil.example"),
        ("GET", "users/me/../profile"),
    ],
)
async def test_no_arbitrary_urls_or_writes(google, caller, method, path):
    with pytest.raises(HealthError):
        await google.request(caller.credentials, caller.counters, method, path)
