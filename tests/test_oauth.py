import asyncio
import json
import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from health_mcp.catalog import SCOPES
from health_mcp.errors import HealthError
from health_mcp.google import GoogleClient
from health_mcp.oauth import challenge
from health_mcp.server import Runtime, make_app
from health_mcp.store import digest
from health_mcp.vault import TokenVault

ORIGIN = "https://health.test"
REDIRECT = "http://127.0.0.1:49123/callback"
VERIFIER = "a" * 64


@pytest.fixture
async def oauth(settings, store, caller, tmp_path):
    key = tmp_path / "key.json"
    key.write_text(TokenVault.create_key_ring())
    settings = replace(
        settings,
        auth_mode="oauth",
        public_url=ORIGIN,
        token_key_file=key,
        allowed_hosts=("health.test",),
        allowed_origins=(ORIGIN,),
    )
    state = {"account": "alice-google", "scopes": SCOPES, "refresh": "private-google-refresh"}

    def upstream(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "private-google-access",
                    "expires_in": 3600,
                    "refresh_token": state["refresh"],
                    "scope": " ".join(state["scopes"]),
                },
            )
        if request.url.path.endswith("/identity"):
            return httpx.Response(200, json={"healthUserId": state["account"]})
        return httpx.Response(200, json={"dataPoints": [{"steps": {"count": "10"}}]})

    google = GoogleClient(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    runtime = Runtime(settings, store=store, google=google)
    application = make_app(runtime)
    starlette = application
    while not hasattr(starlette, "router"):
        starlette = starlette.app
    ready, stop = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with starlette.router.lifespan_context(starlette):
            ready.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await ready.wait()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url=ORIGIN
    ) as client:
        yield client, runtime, state
    stop.set()
    await task


async def register(client, **overrides):
    response = await client.post(
        "/register",
        json={
            "client_name": "Test client",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "health:read",
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def begin(client, registration=None):
    registration = registration or await register(client)
    response = await client.get(
        "/authorize",
        params={
            "client_id": registration["client_id"],
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": ORIGIN + "/mcp",
        },
    )
    assert response.status_code == 302
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
    consent = await client.get(response.headers["location"])
    assert consent.status_code == 200
    assert consent.headers["referrer-policy"] == "same-origin"
    csrf = re.search(r'name="csrf" value="([^"]+)"', consent.text)[1]
    return registration, request_id, csrf


async def google_redirect(client, invite="alice-mcp", registration=None):
    registration, request_id, csrf = await begin(client, registration)
    response = await client.post(
        "/oauth/consent",
        headers={"Origin": ORIGIN},
        data={
            "request": request_id,
            "csrf": csrf,
            "invite": invite,
        },
    )
    assert response.status_code == 303, response.text
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [ORIGIN + "/oauth/google/callback"]
    return registration, params["state"][0]


async def authorization_code(client, invite="alice-mcp", registration=None):
    registration, state = await google_redirect(client, invite, registration)
    response = await client.get(
        "/oauth/google/callback", params={"state": state, "code": "fake-google-code"}
    )
    assert response.status_code == 303, response.text
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["state"] == ["client-state"]
    return registration, params["code"][0]


async def exchange(client, registration, code, **overrides):
    return await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": ORIGIN + "/mcp",
            **overrides,
        },
    )


async def test_oauth_end_to_end_sdk_and_encrypted_storage(oauth):
    client, runtime, state = oauth
    discovery = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert discovery.json()["resource"] == ORIGIN + "/mcp"
    registration, code = await authorization_code(client)
    response = await exchange(client, registration, code)
    assert response.status_code == 200
    tokens = response.json()
    assert state["refresh"] not in response.text
    client.headers["Authorization"] = "Bearer " + tokens["access_token"]
    async with streamable_http_client(ORIGIN + "/mcp", http_client=client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("query_data", {"data_type": "steps"})
            assert not result.isError
    with runtime.store.connect() as db:
        dump = "\n".join(db.iterdump())
        row = db.execute("SELECT * FROM google_connections").fetchone()
    for secret in (
        state["refresh"],
        "private-google-access",
        tokens["access_token"],
        tokens["refresh_token"],
        code,
    ):
        assert secret not in dump
    assert runtime.oauth.repository.credentials(row["owner"]).refresh_token == state["refresh"]


async def test_code_pkce_resource_client_binding_and_single_use(oauth):
    client, _, _ = oauth
    registration, code = await authorization_code(client)
    assert (await exchange(client, registration, code, code_verifier="wrong")).status_code == 400
    assert (
        await exchange(client, registration, code, resource="https://other.test/mcp")
    ).status_code == 400
    other = await register(client)
    assert (await exchange(client, other, code)).status_code == 400
    responses = await asyncio.gather(
        exchange(client, registration, code), exchange(client, registration, code)
    )
    assert sorted(r.status_code for r in responses) == [200, 400]


async def test_refresh_rotation_replay_revokes_family(oauth):
    client, runtime, _ = oauth
    registration, code = await authorization_code(client)
    old = (await exchange(client, registration, code)).json()
    request = {
        "grant_type": "refresh_token",
        "client_id": registration["client_id"],
        "refresh_token": old["refresh_token"],
    }
    response = await client.post("/token", data=request)
    assert response.status_code == 200
    new = response.json()
    assert new["refresh_token"] != old["refresh_token"]
    assert await runtime.oauth.load_access_token(old["access_token"]) is None
    assert await runtime.oauth.load_access_token(new["access_token"]) is not None
    assert (await client.post("/token", data=request)).status_code == 400
    assert await runtime.oauth.load_access_token(new["access_token"]) is None


async def test_consent_csrf_state_and_callback_browser_binding(oauth):
    client, _, _ = oauth
    registration, request_id, csrf = await begin(client)
    data = {"request": request_id, "csrf": csrf, "invite": "alice-mcp"}
    assert (await client.post("/oauth/consent", data=data)).status_code == 400
    assert (
        await client.post(
            "/oauth/consent", headers={"Origin": ORIGIN}, data={**data, "csrf": "wrong"}
        )
    ).status_code == 400
    response = await client.post("/oauth/consent", headers={"Origin": ORIGIN}, data=data)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    cookie = client.cookies.get("__Host-health-login")
    client.cookies.clear()
    assert (
        await client.get("/oauth/google/callback", params={"state": state, "code": "fake"})
    ).status_code == 400
    client.cookies.set("__Host-health-login", cookie)
    assert (
        await client.get("/oauth/google/callback", params={"state": "wrong", "code": "fake"})
    ).status_code == 400
    assert (
        await client.get("/oauth/google/callback", params={"state": state, "code": "fake"})
    ).status_code == 303
    assert (
        await client.get("/oauth/google/callback", params={"state": state, "code": "fake"})
    ).status_code == 400


async def test_invitation_account_binding_and_existing_user_login(oauth):
    client, runtime, state = oauth
    _, google_state = await google_redirect(client, invite="")
    denied = await client.get(
        "/oauth/google/callback", params={"state": google_state, "code": "fake"}
    )
    assert denied.status_code == 400 and "INVITATION_REQUIRED" in denied.text
    await authorization_code(client)
    await authorization_code(client, invite="")
    state["account"] = "another-google-account"
    _, google_state = await google_redirect(client)
    denied = await client.get(
        "/oauth/google/callback", params={"state": google_state, "code": "fake"}
    )
    assert denied.status_code == 400 and "GOOGLE_ACCOUNT_MISMATCH" in denied.text
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM google_connections").fetchone()[0] == 1


async def test_oauth_rejects_legacy_bearer_and_forwarded_google(oauth):
    client, _, _ = oauth
    response = await client.post(
        "/mcp",
        json={},
        headers={"Authorization": "Bearer alice-mcp", "X-Google-Refresh-Token": "owner-refresh"},
    )
    assert response.status_code == 401
    assert 'resource_metadata="' in response.headers["www-authenticate"]


async def test_revocation_disable_and_disconnect(oauth):
    client, runtime, _ = oauth
    registration, code = await authorization_code(client)
    tokens = (await exchange(client, registration, code)).json()
    assert (
        await client.post(
            "/revoke",
            data={"client_id": registration["client_id"], "token": tokens["refresh_token"]},
        )
    ).status_code == 200
    assert await runtime.oauth.load_access_token(tokens["access_token"]) is None
    registration, code = await authorization_code(client, invite="")
    tokens = (await exchange(client, registration, code)).json()
    access = await runtime.oauth.load_access_token(tokens["access_token"])
    runtime.oauth.repository.revoke_user(access.subject)
    assert await runtime.oauth.load_access_token(tokens["access_token"]) is None
    with pytest.raises(HealthError):
        runtime.oauth.repository.credentials(access.subject)


def test_vault_context_binding_and_key_rotation(tmp_path):
    key_file = tmp_path / "keys.json"
    ring = json.loads(TokenVault.create_key_ring())
    key_file.write_text(json.dumps(ring))
    vault = TokenVault(key_file)
    envelope = vault.seal("secret", "google:alice:client")
    with pytest.raises(HealthError):
        vault.open(envelope, "google:bob:client")
    ring["keys"]["v2"] = json.loads(TokenVault.create_key_ring())["keys"]["v1"]
    ring["active"] = "v2"
    key_file.write_text(json.dumps(ring))
    rotated = TokenVault(key_file)
    assert rotated.open(envelope, "google:alice:client") == "secret"
    assert json.loads(rotated.seal("secret", "google:alice:client"))["version"] == "v2"


async def test_scope_checks_expiry_and_unsafe_redirects(oauth):
    client, runtime, state = oauth
    state["scopes"] = [SCOPES[0]]
    _, google_state = await google_redirect(client)
    response = await client.get(
        "/oauth/google/callback", params={"state": google_state, "code": "fake"}
    )
    assert response.status_code == 400 and "GOOGLE_SCOPES_MISSING" in response.text
    response = await client.post(
        "/register",
        json={
            "redirect_uris": ["http://evil.test/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert response.status_code == 400
    state["scopes"] = SCOPES
    registration, code = await authorization_code(client)
    with runtime.store.connect() as db:
        db.execute("UPDATE oauth_codes SET expires_at=0 WHERE hash=?", (digest(code),))
    assert (await exchange(client, registration, code)).status_code == 400


async def test_two_users_cannot_read_each_others_exports(oauth):
    client, runtime, state = oauth
    registration, code = await authorization_code(client)
    alice = (await exchange(client, registration, code)).json()
    client.headers["Authorization"] = "Bearer " + alice["access_token"]
    async with streamable_http_client(ORIGIN + "/mcp", http_client=client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("export_data", {"data_type": "steps"})
            assert not result.isError
            exported = result.structuredContent or json.loads(result.content[0].text)
    assert (await client.get(exported["download_path"])).status_code == 200

    runtime.store.add_user("bob", "bob-mcp")
    state["account"], state["refresh"] = "bob-google", "bob-google-refresh"
    registration, code = await authorization_code(client, invite="bob-mcp")
    bob = (await exchange(client, registration, code)).json()
    client.headers["Authorization"] = "Bearer " + bob["access_token"]
    # Supplying somebody else's Google token cannot override the authenticated owner.
    client.headers["X-Google-Refresh-Token"] = "private-google-refresh"
    assert (await client.get(exported["download_path"])).status_code == 404
    async with streamable_http_client(ORIGIN + "/mcp", http_client=client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("read_export", {"export_id": exported["export_id"]})
            assert result.isError
    assert await runtime.oauth.load_access_token(alice["access_token"]) is not None
    runtime.store.disable_user("bob")
    assert (await client.post("/mcp", json={})).status_code == 401
    assert await runtime.oauth.load_access_token(alice["access_token"]) is not None


async def test_malformed_public_requests_and_expired_access(oauth):
    client, runtime, _ = oauth
    for path in ("/token", "/revoke"):
        response = await client.post(
            path, content=b"\xff", headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        assert response.status_code == 400
    registration, code = await authorization_code(client)
    tokens = (await exchange(client, registration, code)).json()
    with runtime.store.connect() as db:
        db.execute(
            "UPDATE oauth_grants SET expires_at=0 WHERE hash=?", (digest(tokens["access_token"]),)
        )
    response = await client.post(
        "/mcp", json={}, headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert response.status_code == 401
