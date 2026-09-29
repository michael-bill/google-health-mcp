"""MCP OAuth provider with explicit consent and a separate Google OAuth grant."""

import base64
import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .catalog import SCOPES
from .errors import HealthError
from .oauth_store import OAuthStore
from .store import digest

MCP_SCOPE = "health:read"
SECURE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'",
}


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def page(title: str, body: str, status: int = 200):
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{font:17px system-ui;max-width:620px;margin:8vh auto;padding:24px;line-height:1.6;"
        "color:#17322d;background:#f7faf8}h1{line-height:1.2}input,button{font:inherit;padding:12px;"
        "box-sizing:border-box;width:100%;margin:10px 0}button{background:#175e4c;color:white;"
        "border:0;border-radius:8px;cursor:pointer}code{overflow-wrap:anywhere}"
        "</style><main>" + f"<h1>{html.escape(title)}</h1>{body}</main></html>",
        status_code=status,
        # no-referrer makes browsers send Origin:null for ordinary form POSTs.
        # Keep same-origin form origins while withholding referrers from Google.
        headers={**SECURE_HEADERS, "Referrer-Policy": "same-origin"},
    )


class OAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, settings, repository: OAuthStore, http):
        self.settings, self.repository, self.http = settings, repository, http
        self.store = repository.store
        self.origin = settings.public_url
        self.resource = self.origin + "/mcp"
        self.callback_url = self.origin + "/oauth/google/callback"

    async def get_client(self, client_id: str):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload FROM oauth_clients WHERE id=?", (client_id,)
            ).fetchone()
        if row:
            payload = self.repository.vault.open(row["payload"], "client:" + client_id)
            return OAuthClientInformationFull.model_validate_json(payload)
        return None

    async def register_client(self, client_info: OAuthClientInformationFull):
        redirects = client_info.redirect_uris or []
        if not 1 <= len(redirects) <= 10:
            raise RegistrationError("invalid_redirect_uri", "Provide 1 to 10 redirect URIs")
        for redirect in redirects:
            url = urlsplit(str(redirect))
            secure = url.scheme == "https" and bool(url.hostname)
            loopback = url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
            if not (secure or loopback) or url.fragment or url.username or url.password:
                raise RegistrationError(
                    "invalid_redirect_uri", "Use HTTPS or an HTTP loopback callback"
                )
            if len(str(redirect)) > 2048:
                raise RegistrationError("invalid_redirect_uri", "Redirect URI too long")
        if len(client_info.client_name or "") > 200:
            raise RegistrationError("invalid_client_metadata", "Client name too long")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= 1000:
                raise RegistrationError("invalid_client_metadata", "Registration capacity reached")
            db.execute(
                "INSERT INTO oauth_clients VALUES(?,?,?)",
                (
                    client_info.client_id,
                    self.repository.vault.seal(
                        client_info.model_dump_json(), "client:" + client_info.client_id
                    ),
                    time.time(),
                ),
            )

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource not in (None, self.resource):
            raise AuthorizeError("invalid_request", "Incorrect resource")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "S256 PKCE is required")
        if params.scopes and params.scopes != [MCP_SCOPE]:
            raise AuthorizeError("invalid_scope", "Unsupported scope")
        params.resource, params.scopes = self.resource, [MCP_SCOPE]
        request_id = secrets.token_urlsafe(32)
        self.repository.prune()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM oauth_pending").fetchone()[0] >= 500:
                raise AuthorizeError("temporarily_unavailable", "Try again later")
            db.execute(
                "INSERT INTO oauth_pending(id,client_id,params,expires_at) VALUES(?,?,?,?)",
                (digest(request_id), client.client_id, params.model_dump_json(), time.time() + 600),
            )
        return self.origin + "/oauth/consent?request=" + request_id

    def pending(self, request_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM oauth_pending WHERE id=? AND expires_at>?",
                (digest(request_id), time.time()),
            ).fetchone()
        if not row:
            raise HealthError("AUTHORIZATION_EXPIRED_RESTART_LOGIN")
        return dict(row)

    async def consent(self, request: Request):
        try:
            if request.method == "GET":
                request_id = request.query_params.get("request", "")
                pending = self.pending(request_id)
                client = await self.get_client(pending["client_id"])
                params = AuthorizationParams.model_validate_json(pending["params"])
                csrf = secrets.token_urlsafe(32)
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE oauth_pending SET csrf_hash=? WHERE id=? AND google_state_hash IS NULL",
                        (digest(csrf), pending["id"]),
                    )
                body = (
                    f"<p>Allow <strong>{html.escape(client.client_name or 'MCP client')}</strong> "
                    "to read your Fitbit / Google Health data through this server?</p>"
                    f"<p>The client callback is <code>{html.escape(str(params.redirect_uri))}</code>.</p>"
                    "<p>This grants access to activity, health measurements, sleep, profile, "
                    "settings, nutrition and workout routes. Google credentials are encrypted "
                    "on this server. Health records may be cached and exported.</p>"
                    '<form method="post" action="/oauth/consent">'
                    f'<input type="hidden" name="request" value="{html.escape(request_id)}">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    "<label>Invitation key (first connection only)"
                    '<input type="password" name="invite" autocomplete="off" maxlength="256"></label>'
                    "<p>Already connected? Leave the invitation empty and choose the same Google account.</p>"
                    '<button type="submit">Allow and continue to Google</button></form>'
                    "<p>Close this page to cancel. Only continue if you initiated this connection.</p>"
                )
                response = page("Connect Google Health", body)
                response.set_cookie(
                    "__Host-health-consent",
                    csrf,
                    secure=True,
                    httponly=True,
                    samesite="lax",
                    max_age=600,
                )
                return response
            if request.headers.get("origin") != self.origin:
                raise HealthError("CONSENT_ORIGIN_INVALID")
            form = await request.form()
            pending = self.pending(str(form.get("request", "")))
            csrf = str(form.get("csrf", ""))
            if (
                not csrf
                or not secrets.compare_digest(
                    csrf, request.cookies.get("__Host-health-consent", "")
                )
                or digest(csrf) != pending["csrf_hash"]
            ):
                raise HealthError("CONSENT_CSRF_INVALID")
            invitation = str(form.get("invite", ""))
            uid = self.store.authenticate(invitation) if invitation else None
            if invitation and not uid:
                raise HealthError("INVITATION_INVALID")
            google_state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            with self.store.connect() as db:
                changed = db.execute(
                    "UPDATE oauth_pending SET google_state_hash=?,browser_hash=?,encrypted_verifier=?,invite_owner=? "
                    "WHERE id=? AND google_state_hash IS NULL AND expires_at>?",
                    (
                        digest(google_state),
                        digest(browser),
                        self.repository.vault.seal(verifier, "pending:" + pending["id"]),
                        uid,
                        pending["id"],
                        time.time(),
                    ),
                ).rowcount
            if not changed:
                raise HealthError("AUTHORIZATION_ALREADY_STARTED")
            url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
                {
                    "client_id": self.settings.client_id,
                    "redirect_uri": self.callback_url,
                    "response_type": "code",
                    "scope": " ".join(SCOPES),
                    "state": google_state,
                    "access_type": "offline",
                    "prompt": "consent",
                    "code_challenge_method": "S256",
                    "code_challenge": challenge(verifier),
                }
            )
            response = RedirectResponse(url, 303, headers=SECURE_HEADERS)
            # Bind the Google callback to this browser only after explicit MCP consent.
            response.set_cookie(
                "__Host-health-login",
                browser,
                secure=True,
                httponly=True,
                samesite="lax",
                max_age=600,
            )
            response.delete_cookie("__Host-health-consent", secure=True, httponly=True)
            return response
        except HealthError as exc:
            return page(
                "Connection not completed",
                f"<p>{exc.code}</p><p>Restart login from your MCP client.</p>",
                400,
            )

    async def callback(self, request: Request):
        try:
            state = request.query_params.get("state", "")
            browser = request.cookies.get("__Host-health-login", "")
            if not state or not browser:
                raise HealthError("OAUTH_STATE_INVALID")
            with self.store.connect() as db:
                row = db.execute(
                    "DELETE FROM oauth_pending WHERE google_state_hash=? AND browser_hash=? AND expires_at>? RETURNING *",
                    (digest(state), digest(browser), time.time()),
                ).fetchone()
            if not row:
                raise HealthError("OAUTH_STATE_INVALID")
            if request.query_params.get("error") or not request.query_params.get("code"):
                raise HealthError("GOOGLE_CONSENT_NOT_GRANTED")
            verifier = self.repository.vault.open(row["encrypted_verifier"], "pending:" + row["id"])
            response = await self.http.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "authorization_code",
                    "code": request.query_params["code"],
                    "redirect_uri": self.callback_url,
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "code_verifier": verifier,
                },
            )
            if response.status_code != 200:
                raise HealthError("GOOGLE_CODE_EXCHANGE_FAILED")
            tokens = response.json()
            granted = tokens.get("scope", "").split()
            if not set(SCOPES).issubset(granted):
                raise HealthError("GOOGLE_SCOPES_MISSING_RECONNECT")
            identity = await self.http.get(
                "https://health.googleapis.com/v4/users/me/identity",
                headers={"Authorization": "Bearer " + tokens["access_token"]},
            )
            if identity.status_code != 200 or not identity.json().get("healthUserId"):
                raise HealthError("GOOGLE_IDENTITY_UNAVAILABLE")
            uid = self.repository.save_connection(
                identity.json()["healthUserId"],
                tokens.get("refresh_token"),
                granted,
                row["invite_owner"],
            )
            params = AuthorizationParams.model_validate_json(row["params"])
            code = secrets.token_urlsafe(32)
            auth_code = AuthorizationCode(
                code="",
                client_id=row["client_id"],
                scopes=params.scopes,
                expires_at=time.time() + 120,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.resource,
                subject=uid,
            )
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO oauth_codes VALUES(?,?,?)",
                    (digest(code), auth_code.model_dump_json(), auth_code.expires_at),
                )
            response = RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                303,
                headers=SECURE_HEADERS,
            )
        except HealthError as exc:
            response = page(
                "Connection not completed",
                f"<p>{exc.code}</p><p>Restart login from your MCP client.</p>",
                400,
            )
        except Exception:
            # Never expose Google's token response or credential-bearing exceptions.
            response = page(
                "Connection not completed",
                "<p>Google connection failed. Restart login from your MCP client.</p>",
                400,
            )
        response.delete_cookie("__Host-health-login", secure=True, httponly=True)
        return response

    async def load_authorization_code(self, client, authorization_code):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload FROM oauth_codes WHERE hash=? AND expires_at>?",
                (digest(authorization_code), time.time()),
            ).fetchone()
        if row:
            result = AuthorizationCode.model_validate_json(row["payload"])
            if result.client_id == client.client_id:
                result.code = authorization_code
                return result
        return None

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "DELETE FROM oauth_codes WHERE hash=? AND expires_at>? RETURNING payload",
                (digest(authorization_code.code), time.time()),
            ).fetchone()
            if not row:
                raise TokenError("invalid_grant", "Code already used or expired")
            saved = AuthorizationCode.model_validate_json(row["payload"])
            enabled = db.execute(
                "SELECT id FROM users WHERE id=? AND enabled=1", (saved.subject,)
            ).fetchone()
            if saved.client_id != client.client_id or not enabled:
                raise TokenError("invalid_grant", "Grant unavailable")
            result = self.repository.issue(
                db, saved.subject, client.client_id, saved.scopes, self.resource
            )
        return OAuthToken(**result)

    async def load_refresh_token(self, client, refresh_token):
        row = self.repository.grant(refresh_token, "refresh", include_revoked=True)
        if not row or row["client_id"] != client.client_id:
            return None
        if row["revoked"]:
            with self.store.connect() as db:
                db.execute("UPDATE oauth_grants SET revoked=1 WHERE family=?", (row["family"],))
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["owner"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        failed = False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth_grants WHERE hash=? AND kind='refresh'",
                (digest(refresh_token.token),),
            ).fetchone()
            enabled = db.execute(
                "SELECT id FROM users WHERE id=? AND enabled=1", (refresh_token.subject,)
            ).fetchone()
            if not row or row["client_id"] != client.client_id or not enabled:
                raise TokenError("invalid_grant", "Grant unavailable")
            if row["revoked"] or row["expires_at"] <= time.time():
                failed = True
            elif not set(scopes).issubset(json.loads(row["scopes"])):
                raise TokenError("invalid_scope", "Scope escalation forbidden")
            db.execute("UPDATE oauth_grants SET revoked=1 WHERE family=?", (row["family"],))
            if not failed:
                result = self.repository.issue(
                    db, row["owner"], client.client_id, scopes, row["resource"], row["family"]
                )
        if failed:
            raise TokenError("invalid_grant", "Refresh token already used or expired")
        return OAuthToken(**result)

    async def load_access_token(self, token):
        row = self.repository.grant(token, "access")
        if (
            not row
            or row["resource"] != self.resource
            or MCP_SCOPE not in json.loads(row["scopes"])
        ):
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["owner"],
        )

    async def revoke_token(self, token):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT family FROM oauth_grants WHERE hash=?", (digest(token.token),)
            ).fetchone()
            if row:
                db.execute("UPDATE oauth_grants SET revoked=1 WHERE family=?", (row["family"],))

    def routes(self):
        routes = create_auth_routes(
            self,
            AnyHttpUrl(self.origin),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        routes.extend(
            create_protected_resource_routes(
                AnyHttpUrl(self.resource),
                [AnyHttpUrl(self.origin)],
                [MCP_SCOPE],
                "Google Health MCP",
            )
        )
        routes.extend(
            [
                Route("/oauth/consent", self.consent, methods=["GET", "POST"]),
                Route("/oauth/google/callback", self.callback),
                Route(
                    "/",
                    lambda request: page(
                        "Google Health MCP",
                        "<p>Connect an MCP client to <code>/mcp</code> to sign in.</p><p>New users need an invitation from the server owner.</p>",
                    ),
                ),
            ]
        )
        return routes


class OAuthBoundary:
    """Limit public OAuth traffic and enforce resource binding on token requests."""

    def __init__(self, app, provider):
        self.app, self.provider = app, provider
        self.buckets = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        public = path in {
            "/",
            "/healthz",
            "/authorize",
            "/token",
            "/register",
            "/revoke",
            "/oauth/consent",
            "/oauth/google/callback",
        } or path.startswith("/.well-known/")
        if not public:
            return await self.app(scope, receive, send)
        now = time.monotonic()
        key = ((scope.get("client") or ("unknown",))[0], path)
        since, count = self.buckets.get(key, (now, 0))
        if now - since >= 60:
            since, count = now, 0
        if len(self.buckets) >= 2000 and key not in self.buckets:
            self.buckets.pop(next(iter(self.buckets)))
        self.buckets[key] = (since, count + 1)
        limit = 10 if path == "/register" else 120
        if count >= limit:
            return await JSONResponse(
                {"error": "rate_limited"}, 429, headers={"Retry-After": "60"}
            )(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 16384:
                return await JSONResponse({"error": "request_too_large"}, 413)(scope, receive, send)
            if not message.get("more_body"):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        # SDK 1.30's revocation model marks the optional client_secret as required.
        # Normalize its absence for public clients without changing authentication.
        if path == "/revoke":
            from urllib.parse import parse_qs

            try:
                form = parse_qs(body.decode(), keep_blank_values=True)
            except (UnicodeError, ValueError):
                return await JSONResponse({"error": "invalid_request"}, 400)(scope, replay, send)
            if "client_secret" not in form:
                body.extend(b"&client_secret=")

        if path == "/token":
            from urllib.parse import parse_qs

            try:
                form = parse_qs(body.decode(), strict_parsing=False)
                resources = form.get("resource", [self.provider.resource])
                if resources != [self.provider.resource]:
                    return await JSONResponse({"error": "invalid_target"}, 400)(scope, replay, send)
            except (UnicodeError, ValueError):
                return await JSONResponse({"error": "invalid_request"}, 400)(scope, replay, send)
        try:
            return await self.app(scope, replay, send)
        except Exception:
            return await JSONResponse(
                {"error": "oauth_request_failed"}, 400, headers=SECURE_HEADERS
            )(scope, replay, send)
