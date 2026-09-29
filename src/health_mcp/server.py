"""Thin MCP contracts and authenticated HTTP transport."""

import asyncio
import re
import time
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp

from .catalog import CATALOG, SCOPES
from .errors import HealthError
from .google import Counters, Credentials, GoogleClient
from .service import Caller, HealthService
from .store import Store


class Runtime:
    def __init__(self, settings, store=None, google=None, local_caller=None):
        self.settings = settings
        self.store = store or Store(settings.database)
        self.google = google or GoogleClient()
        self.service = HealthService(settings, self.store, self.google)
        self.local_caller = local_caller
        self.oauth = None
        if settings.auth_mode == "oauth":
            from .oauth import OAuthProvider
            from .oauth_store import OAuthStore
            from .vault import TokenVault

            repository = OAuthStore(
                self.store,
                TokenVault(settings.token_key_file),
                settings.client_id,
                settings.client_secret,
            )
            self.oauth = OAuthProvider(settings, repository, self.google.http)

    def caller(self, ctx: Context) -> Caller:
        request = ctx.request_context.request
        if request is None:
            if self.local_caller is None:
                raise HealthError("LOCAL_USER_NOT_CONFIGURED")
            uid, creds = self.local_caller
            return Caller(uid, creds, Counters())
        uid = request.scope.get("health_user")
        if uid is None:
            raise HealthError("MCP_AUTHENTICATION_REQUIRED")
        if self.oauth:
            return Caller(uid, self.oauth.repository.credentials(uid), Counters())
        headers = request.headers
        # A remote caller NEVER inherits the owner's refresh token.
        cid = headers.get("x-google-client-id")
        secret = headers.get("x-google-client-secret")
        if bool(cid) != bool(secret):
            raise HealthError("BOTH_GOOGLE_CLIENT_CREDENTIALS_REQUIRED")
        creds = Credentials(
            cid or self.settings.client_id,
            secret or self.settings.client_secret,
            headers.get("x-google-refresh-token", ""),
        )
        return Caller(uid, creds, Counters())

    async def invoke(self, tool, ctx, handler, google_required=True):
        started = time.monotonic()
        caller = None
        error = None
        outcome = "ok"
        try:
            caller = self.caller(ctx)
            if google_required:
                await self.service.check_identity(caller)
            result = await handler(caller)
            if isinstance(result, dict) and result.get("error"):
                outcome = "partial"
                error = "PARTIAL_UPSTREAM_FAILURE"
            if (
                isinstance(result, dict)
                and result.get("metrics")
                and any("error" in v for v in result["metrics"].values())
            ):
                outcome = "partial"
                error = "PARTIAL_UPSTREAM_FAILURE"
            return result
        except HealthError as exc:
            outcome = "error"
            error = exc.code
            raise ValueError(exc.code) from None
        except asyncio.CancelledError:
            outcome = "cancelled"
            error = "REQUEST_CANCELLED"
            raise
        except Exception:
            outcome = "error"
            error = "INTERNAL_ERROR"
            raise ValueError("INTERNAL_ERROR") from None
        finally:
            self.store.event(
                caller.user_id if caller else None,
                tool,
                outcome,
                (time.monotonic() - started) * 1000,
                caller.counters if caller else Counters(),
                error,
            )


def make_server(runtime: Runtime) -> FastMCP:
    s = runtime.settings

    @asynccontextmanager
    async def lifespan(server):
        # FastMCP may enter this once per stateless HTTP request. Shared clients
        # must be closed by the ASGI application, not by an individual request.
        yield runtime

    mcp = FastMCP(
        "google-health",
        instructions=(
            "Read-only Google Health access. Start with list_data_types or get_profile. "
            "Ranges are start-inclusive, end-exclusive. Date-only bounds use recorded civil time. "
            "Follow next_cursor until complete; partial or empty data is not a zero measurement. "
            "Never request credentials as tool arguments. Results contain personal health data."
        ),
        host=s.host,
        port=s.port,
        stateless_http=True,
        json_response=True,
        log_level="CRITICAL",
        lifespan=lifespan,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(s.allowed_hosts),
            allowed_origins=list(s.allowed_origins),
        ),
    )
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @mcp.tool(annotations=ro)
    async def list_data_types(ctx: Context) -> dict:
        """List all 37 supported types, read operations and required scopes. No health records are fetched."""

        async def run(c):
            return {
                "types": list(CATALOG.values()),
                "scopes": SCOPES,
                "route_export": "export_workout_route",
            }

        return await runtime.invoke("list_data_types", ctx, run, False)

    @mcp.tool(annotations=ro)
    async def get_profile(ctx: Context) -> dict:
        """Get the profile, including age when provided by Google."""
        return await runtime.invoke(
            "get_profile", ctx, lambda c: runtime.service.account(c, "profile")
        )

    @mcp.tool(annotations=ro)
    async def get_settings(ctx: Context) -> dict:
        """Get account timezone, units and other available settings."""
        return await runtime.invoke(
            "get_settings", ctx, lambda c: runtime.service.account(c, "settings")
        )

    @mcp.tool(annotations=ro)
    async def get_devices(ctx: Context) -> dict:
        """Get paired devices and their last sync times."""
        return await runtime.invoke(
            "get_devices", ctx, lambda c: runtime.service.account(c, "pairedDevices")
        )

    @mcp.tool(annotations=ro)
    async def get_data_status(ctx: Context) -> dict:
        """Validate Google access and report granted scopes. This does not assert that measurements exist."""

        async def run(c):
            access = await runtime.google.access(c.credentials, c.counters)
            return {
                "connected": True,
                "granted_scopes": access.scopes,
                "configured_scopes": SCOPES,
                "supported_data_types": len(CATALOG),
                "historical_coverage": "Check the requested type and period with query_data",
            }

        return await runtime.invoke("get_data_status", ctx, run)

    @mcp.tool(annotations=ro)
    async def query_data(
        ctx: Context,
        data_type: str,
        start: str | None = None,
        end: str | None = None,
        mode: str = "auto",
        page_size: int = 100,
        source: str | None = None,
        window_seconds: int = 3600,
    ) -> dict:
        """Read any catalog type. Modes: auto, list, reconcile, dailyRollup, rollup.
        start inclusive/end exclusive: YYYY-MM-DD civil dates, or timezone-aware timestamps where supported.
        Sleep selects sessions by END date. Daily metrics require dates. rollup requires timestamps;
        dailyRollup requires dates. Food catalogs have no date filter. auto prefers reconcile.
        Follow next_cursor with next_page, including when a page is empty. Unit fields are preserved.
        Source: all-sources, google-wearables, google-sources or self-sources; omit unless needed.
        """
        return await runtime.invoke(
            "query_data",
            ctx,
            lambda c: runtime.service.query(
                c,
                name=data_type,
                start=start,
                end=end,
                mode=mode,
                page_size=page_size,
                source=source,
                window_seconds=window_seconds,
            ),
        )

    @mcp.tool(annotations=ro)
    async def next_page(ctx: Context, cursor: str) -> dict:
        """Continue a query using its opaque, user-bound next_cursor. Cursors expire after 24 hours."""
        return await runtime.invoke(
            "next_page", ctx, lambda c: runtime.service.next_page(c, cursor)
        )

    @mcp.tool(annotations=ro)
    async def get_record(ctx: Context, data_type: str, record_id: str) -> dict:
        """Get a complete individual record for catalog types supporting get. Use the final ID segment of its name."""
        return await runtime.invoke(
            "get_record",
            ctx,
            lambda c: runtime.service.get_record(c, data_type, record_id),
        )

    @mcp.tool(annotations=ro)
    async def get_sleep(ctx: Context, start: str, end: str, page_size: int = 25) -> dict:
        """Get reconciled sleep sessions and stages, selected by session END date/time. End is exclusive."""
        return await runtime.invoke(
            "get_sleep",
            ctx,
            lambda c: runtime.service.query(
                c, name="sleep", start=start, end=end, page_size=page_size
            ),
        )

    @mcp.tool(annotations=ro)
    async def get_workouts(ctx: Context, start: str, end: str, page_size: int = 25) -> dict:
        """Get reconciled workouts within civil date bounds. Routes are fetched separately on explicit request."""
        return await runtime.invoke(
            "get_workouts",
            ctx,
            lambda c: runtime.service.query(
                c, name="exercise", start=start, end=end, page_size=page_size
            ),
        )

    @mcp.tool(annotations=ro)
    async def get_health_summary(ctx: Context, start: str, end: str) -> dict:
        """Get sleep, daily steps, resting HR, HRV, SpO2, respiratory rate and workouts for civil dates.
        This overview is bounded: follow per-metric cursors when complete=false. Errors and missing data
        are explicit. No clinical diagnosis or reference ranges are calculated.
        """
        return await runtime.invoke(
            "get_health_summary", ctx, lambda c: runtime.service.summary(c, start, end)
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    )
    async def export_data(
        ctx: Context,
        data_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
        mode: str = "auto",
        export_id: str | None = None,
        max_pages: int = 10,
    ) -> dict:
        """Export records into a private JSONL file on the server. Reads Google only; writes local storage.
        Continue large exports by calling again with export_id until complete=true. A batch uses at most
        20 pages. Download via the returned path with your MCP Authorization header, outside model context.
        Continuations must run within 24 hours of the previous batch.
        """
        return await runtime.invoke(
            "export_data",
            ctx,
            lambda c: runtime.service.export(c, data_type, start, end, mode, export_id, max_pages),
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    )
    async def export_workout_route(ctx: Context, exercise_id: str) -> dict:
        """Explicitly export a workout's TCX (may include sensitive GPS coordinates) to a private server file.
        Requires activity and location permissions as enforced by Google. Download with MCP authentication.
        """
        return await runtime.invoke(
            "export_workout_route", ctx, lambda c: runtime.service.route(c, exercise_id)
        )

    @mcp.tool(annotations=ro)
    async def read_export(
        ctx: Context, export_id: str, offset: int = 0, max_bytes: int = 32000
    ) -> dict:
        """Read your completed JSONL/TCX export, including GPS when requested.
        This sends private export contents into model context. Follow next_offset until complete.
        Offsets are byte positions; only use zero or a returned next_offset.
        """

        async def run(caller):
            return runtime.service.read_export(caller, export_id, offset, max_bytes)

        return await runtime.invoke("read_export", ctx, run)

    return mcp


class AuthenticatedApp:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if scope["path"] == "/healthz":
            return await JSONResponse({"status": "ok"})(scope, receive, send)
        oauth = self.runtime.oauth
        if oauth and (
            scope["path"]
            in {
                "/",
                "/authorize",
                "/token",
                "/register",
                "/revoke",
                "/oauth/consent",
                "/oauth/google/callback",
            }
            or scope["path"].startswith("/.well-known/")
        ):
            return await self.app(scope, receive, send)
        auth = headers.get(b"authorization", b"")
        uid = None
        if auth.startswith(b"Bearer ") and len(auth) < 1024:
            try:
                token = auth[7:].decode("ascii")
                if oauth:
                    access = await oauth.load_access_token(token)
                    uid = access.subject if access else None
                else:
                    uid = self.runtime.store.authenticate(token)
            except UnicodeError:
                pass
        if uid is None:
            self.runtime.store.event(
                None,
                "transport_auth",
                "denied",
                0,
                Counters(),
                "MCP_AUTHENTICATION_REQUIRED",
            )
            return await JSONResponse(
                {"error": "MCP_AUTHENTICATION_REQUIRED"},
                401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{oauth.origin}/.well-known/oauth-protected-resource/mcp", scope="health:read"'
                        if oauth
                        else "Bearer"
                    )
                },
            )(scope, receive, send)
        if sum(len(k) + len(v) for k, v in scope.get("headers", [])) > 32768:
            return await JSONResponse({"error": "HEADERS_TOO_LARGE"}, 431)(scope, receive, send)
        scope["health_user"] = uid
        match = re.fullmatch(r"/exports/([a-f0-9]{32})", scope["path"])
        if match:
            if scope["method"] != "GET":
                return await JSONResponse({"error": "METHOD_NOT_ALLOWED"}, 405)(
                    scope, receive, send
                )
            eid = match[1]
            with self.runtime.store.connect() as db:
                row = db.execute(
                    "SELECT complete FROM exports WHERE id=? AND owner=?", (eid, uid)
                ).fetchone()
            if row and row["complete"]:
                for suffix in (".jsonl", ".tcx"):
                    path = self.runtime.settings.export_dir / (eid + suffix)
                    if path.is_file():
                        self.runtime.store.event(uid, "download_export", "ok", 0, Counters())
                        return await FileResponse(
                            path,
                            filename=eid + suffix,
                            headers={"Cache-Control": "no-store"},
                        )(scope, receive, send)
            return await JSONResponse({"error": "EXPORT_NOT_FOUND_OR_INCOMPLETE"}, 404)(
                scope, receive, send
            )
        return await self.app(scope, receive, send)


def make_app(runtime: Runtime) -> ASGIApp:
    app = make_server(runtime).streamable_http_app()
    if runtime.oauth:
        app.router.routes[:0] = runtime.oauth.routes()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def application_lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            await runtime.google.close()

    app.router.lifespan_context = application_lifespan
    authenticated = AuthenticatedApp(app, runtime)
    if runtime.oauth:
        from urllib.parse import urlsplit

        from starlette.middleware.trustedhost import TrustedHostMiddleware

        from .oauth import OAuthBoundary

        return TrustedHostMiddleware(
            OAuthBoundary(authenticated, runtime.oauth),
            allowed_hosts=[
                urlsplit(runtime.settings.public_url).hostname,
                "127.0.0.1",
                "localhost",
            ],
        )
    return authenticated
