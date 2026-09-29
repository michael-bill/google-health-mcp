import httpx
import pytest

from health_mcp.config import Settings
from health_mcp.google import Counters, Credentials, GoogleClient
from health_mcp.service import Caller, HealthService
from health_mcp.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database=tmp_path / "health.sqlite3",
        export_dir=tmp_path / "exports",
        client_id="test-client",
        client_secret="test-secret",
        refresh_token="owner-refresh",
        allowed_hosts=("testserver", "localhost:*", "127.0.0.1:*"),
    )


@pytest.fixture
def store(settings):
    return Store(settings.database)


@pytest.fixture
def caller(store):
    uid, _ = store.add_user("alice", "alice-mcp")
    return Caller(uid, Credentials("test-client", "test-secret", "alice-refresh"), Counters())


class FakeGoogle:
    def __init__(self):
        self.requests = []
        self.handler = None

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "upstream-access",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
                },
            )
        if request.url.path.endswith("/identity"):
            return httpx.Response(200, json={"healthUserId": "account-alice"})
        if self.handler:
            return self.handler(request)
        return httpx.Response(200, json={"dataPoints": [{"steps": {"count": "10"}}]})


@pytest.fixture
def fake():
    return FakeGoogle()


@pytest.fixture
async def google(fake):
    async def no_sleep(seconds):
        pass

    instance = GoogleClient(httpx.AsyncClient(transport=httpx.MockTransport(fake)), sleep=no_sleep)
    yield instance
    await instance.close()


@pytest.fixture
def service(settings, store, google):
    return HealthService(settings, store, google)
