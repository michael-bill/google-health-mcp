import asyncio
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from health_mcp.server import Runtime, make_app


@pytest.fixture
async def app(settings, store, google, caller):
    runtime = Runtime(settings, store=store, google=google)
    application = make_app(runtime)
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def run_lifespan():
        async with application.app.router.lifespan_context(application.app):
            ready.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await ready.wait()
    try:
        yield application
    finally:
        stop.set()
        await task


async def call(app, tool, args=None, refresh="alice-refresh"):
    headers = {"Authorization": "Bearer alice-mcp"}
    if refresh is not None:
        headers["X-Google-Refresh-Token"] = refresh
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers) as http:
        async with streamable_http_client("http://testserver/mcp", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, args or {})


async def test_http_mcp_handshake_tool_and_telemetry(app, store):
    result = await call(app, "query_data", {"data_type": "steps"})
    assert not result.isError
    with store.connect() as db:
        row = dict(db.execute("SELECT * FROM usage_events WHERE tool='query_data'").fetchone())
    assert row["outcome"] == "ok" and row["google_requests"] == 2 and row["records"] == 1
    assert "alice-refresh" not in json.dumps(row)
    assert "upstream-access" not in json.dumps(row)


async def test_http_does_not_fall_back_to_owner_refresh(app):
    result = await call(app, "get_profile", refresh=None)
    assert result.isError
    assert "GOOGLE_CREDENTIALS_MISSING" in str(result)
    assert "owner-refresh" not in str(result)


async def test_http_authentication_and_revocation(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        assert (await client.post("http://testserver/mcp", json={})).status_code == 401
        store.disable_user("alice")
        assert (
            await client.post(
                "http://testserver/mcp",
                json={},
                headers={"Authorization": "Bearer alice-mcp"},
            )
        ).status_code == 401


async def test_safe_tool_errors_are_logged_without_arguments(app, store, fake):
    fake.handler = lambda req: httpx.Response(403, json={"secret": "do not leak"})
    result = await call(app, "get_profile")
    assert result.isError and "GOOGLE_PERMISSION_DENIED" in str(result)
    assert "do not leak" not in str(result)
    assert store.stats()["errors"][0]["error_code"] == "GOOGLE_PERMISSION_DENIED"


async def test_exports_require_same_user(app, store, settings, caller):
    eid = store.export_create(caller.user_id, caller.credentials.fingerprint(), None)
    settings.export_dir.mkdir()
    (settings.export_dir / (eid + ".jsonl")).write_text('{"private":true}\n')
    with store.connect() as db:
        db.execute("UPDATE exports SET complete=1 WHERE id=?", (eid,))
    store.add_user("bob", "bob-mcp")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        assert (
            await client.get(
                "http://testserver/exports/" + eid,
                headers={"Authorization": "Bearer bob-mcp"},
            )
        ).status_code == 404
        assert (
            await client.get(
                "http://testserver/exports/" + eid,
                headers={"Authorization": "Bearer alice-mcp"},
            )
        ).status_code == 200
