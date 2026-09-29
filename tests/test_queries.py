import json
import logging

import httpx
import pytest

from health_mcp.catalog import CATALOG, build_filter
from health_mcp.errors import HealthError
from health_mcp.google import Counters, Credentials
from health_mcp.service import Caller


async def test_export_returns_checkpoint_after_upstream_failure(service, caller, fake):
    def failing_second_page(request):
        if request.url.params.get("pageToken"):
            return httpx.Response(503, json={"error": "private upstream detail"})
        return httpx.Response(
            200, json={"dataPoints": [{"steps": {"count": "10"}}], "nextPageToken": "second"}
        )

    fake.handler = failing_second_page
    partial = await service.export(caller, name="steps")
    assert partial["records"] == 1
    assert not partial["complete"]
    assert partial["error"]["code"] == "GOOGLE_UPSTREAM_ERROR"
    assert "private upstream detail" not in json.dumps(partial)

    fake.handler = lambda request: httpx.Response(
        200, json={"dataPoints": [{"steps": {"count": "20"}}]}
    )
    complete = await service.export(caller, export_id=partial["export_id"])
    assert complete["complete"]
    assert complete["records"] == 2
    assert "error" not in complete


def test_all_usage_events_are_logged_without_credentials(store, caller, caplog):
    with caplog.at_level(logging.INFO, logger="health_mcp.usage"):
        event = store.event(caller.user_id, "download_export", "ok", 1, caller.counters)
    assert json.loads(caplog.records[-1].message) == event
    assert caller.credentials.refresh_token not in caplog.text


def test_filters_preserve_time_semantics():
    assert "sleep.interval.civil_end_time" in build_filter("sleep", "2026-03-01", "2026-03-02")
    assert "weight.sample_time.physical_time" in build_filter(
        "weight", "2026-03-01T03:00:00+03:00", "2026-03-02T03:00:00+03:00"
    )
    assert "2026-03-01T00:00:00+00:00" in build_filter(
        "weight", "2026-03-01T03:00:00+03:00", "2026-03-02T03:00:00+03:00"
    )
    assert "daily_heart_rate_variability.date" in build_filter(
        "daily-heart-rate-variability", "2026-03-01", "2026-03-02"
    )


@pytest.mark.parametrize(
    "name,start,end",
    [
        ("sleep", "2026-03-02", "2026-03-01"),
        ("sleep", "2026-03-01T01:00:00", "2026-03-02T01:00:00"),
        ("steps", '2026-03-01" OR x', "2026-03-02"),
        ("food", "2026-03-01", "2026-03-02"),
        ("daily-vo2-max", "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z"),
    ],
)
def test_invalid_filters(name, start, end):
    with pytest.raises(HealthError):
        build_filter(name, start, end)


def test_catalog_covers_seven_scope_data_types_without_writes():
    assert len(CATALOG) == 37
    assert CATALOG["food"]["operations"] == ["list", "get"]
    assert CATALOG["total-calories"]["operations"] == ["rollup", "dailyRollup"]
    assert "ecg" not in str(CATALOG)
    assert not {"create", "update", "batchDelete"} & {
        op for row in CATALOG.values() for op in row["operations"]
    }


async def test_pagination_and_empty_page_are_not_silently_truncated(service, caller, fake):
    def handler(req):
        if not req.url.params.get("pageToken"):
            return httpx.Response(200, json={"dataPoints": [], "nextPageToken": "second-page"})
        return httpx.Response(200, json={"dataPoints": [{"sleep": {"duration": "12s"}}]})

    fake.handler = handler
    first = await service.query(caller, name="sleep", start="2026-01-01", end="2026-02-01")
    assert first["count"] == 0 and not first["complete"]
    last = await service.next_page(caller, first["next_cursor"])
    assert last["count"] == 1 and last["complete"]
    assert fake.requests[-1].url.params["pageSize"] == "25"


async def test_cursor_and_cache_are_bound_to_user_and_credentials(service, caller, store, fake):
    fake.handler = lambda r: httpx.Response(
        200, json={"dataPoints": [{"steps": {"count": "8"}}], "nextPageToken": "p2"}
    )
    first = await service.query(caller, name="steps")
    same = await service.query(caller, name="steps")
    assert same["cache_hit"]
    other_id, _ = store.add_user("bob", "bob-mcp")
    other = Caller(other_id, caller.credentials, Counters())
    assert not (await service.query(other, name="steps"))["cache_hit"]
    with pytest.raises(HealthError, match="CURSOR_INVALID"):
        await service.next_page(other, first["next_cursor"])
    changed = Caller(
        caller.user_id, Credentials("test-client", "test-secret", "rotated"), Counters()
    )
    with pytest.raises(HealthError, match="CURSOR_INVALID"):
        await service.next_page(changed, first["next_cursor"])


async def test_rollup_splits_range_and_follows_pages(service, caller, fake):
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        result = {"rollupDataPoints": [{"start": b["range"]["start"]}]}
        if not b.get("pageToken") and b["range"]["start"]["date"]["day"] == 1:
            result["nextPageToken"] = "p2"
        return httpx.Response(200, json=result)

    fake.handler = handler
    page = await service.query(
        caller,
        name="heart-rate",
        start="2026-01-01",
        end="2026-02-01",
        mode="dailyRollup",
    )
    while page["next_cursor"]:
        page = await service.next_page(caller, page["next_cursor"])
    assert len(bodies) == 4
    assert all("pageSize" not in body for body in bodies)
    assert bodies[0]["range"] == bodies[1]["range"]
    assert bodies[0]["range"]["end"]["date"]["day"] == 15
    assert bodies[-1]["range"]["end"]["date"] == {"year": 2026, "month": 2, "day": 1}


async def test_physical_rollup_chunk_alignment(service, caller, fake):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={})

    fake.handler = handler
    page = await service.query(
        caller,
        name="heart-rate",
        start="2026-01-01T00:00:00Z",
        end="2026-02-01T00:00:00Z",
        mode="rollup",
        window_seconds=5 * 86400,
    )
    while page["next_cursor"]:
        page = await service.next_page(caller, page["next_cursor"])
    assert len(bodies) == 4
    assert bodies[0]["range"]["endTime"].startswith("2026-01-11")


async def test_identity_binding_rejects_different_google_account(service, caller, store):
    await service.check_identity(caller)
    assert not store.bind_account(caller.user_id, "somebody-else")


async def test_export_resumes_without_duplicates(service, caller, fake):
    fake.handler = lambda r: httpx.Response(
        200,
        json={
            "dataPoints": [{"n": 2 if r.url.params.get("pageToken") else 1}],
            **({} if r.url.params.get("pageToken") else {"nextPageToken": "p2"}),
        },
    )
    first = await service.export(caller, name="steps", max_pages=1)
    assert not first["complete"] and first["records"] == 1
    path = service.settings.export_dir / (first["export_id"] + ".jsonl")
    with path.open("a") as f:
        f.write('{"uncommitted":true}\n')
    second = await service.export(caller, export_id=first["export_id"], max_pages=1)
    assert second["complete"] and second["records"] == 2
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"n": 1},
        {"n": 2},
    ]
    third = await service.export(caller, export_id=first["export_id"])
    assert third["records"] == 2


async def test_daily_rollup_bounds_output_by_dates(service, caller, fake):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"rollupDataPoints": []})

    fake.handler = handler
    page = await service.query(
        caller, name="total-calories", start="2026-01-01", end="2026-01-08", page_size=2
    )
    while page["next_cursor"]:
        page = await service.next_page(caller, page["next_cursor"])
    assert len(bodies) == 4
    assert bodies[0]["range"]["end"]["date"]["day"] == 3
    assert bodies[-1]["range"]["end"]["date"]["day"] == 8
    assert all("pageSize" not in body for body in bodies)


async def test_export_read_handles_utf8_and_rejects_other_users(service, caller, store):
    export_id = store.export_create(caller.user_id, caller.credentials.fingerprint(), None)
    service.settings.export_dir.mkdir()
    text = "<name>Маршрут</name>"
    (service.settings.export_dir / (export_id + ".tcx")).write_text(text, encoding="utf-8")
    with store.connect() as database:
        database.execute("UPDATE exports SET complete=1 WHERE id=?", (export_id,))
    offset = 0
    received = ""
    while True:
        result = service.read_export(caller, export_id, offset, max_bytes=9)
        received += result["text"]
        if result["complete"]:
            break
        offset = result["next_offset"]
    assert received == text
    other_id, _ = store.add_user("other", "other-mcp")
    other = Caller(other_id, caller.credentials, Counters())
    with pytest.raises(HealthError, match="EXPORT_NOT_FOUND"):
        service.read_export(other, export_id)


async def test_partial_summary_keeps_successful_metrics(service, caller, fake):
    fake.handler = lambda request: httpx.Response(
        403 if "sleep" in request.url.path else 200, json={}
    )
    result = await service.summary(caller, "2026-01-01", "2026-01-02")
    assert not result["complete"]
    assert result["metrics"]["sleep"]["error"]["code"] == "GOOGLE_PERMISSION_DENIED"
    assert result["metrics"]["steps"]["complete"]
