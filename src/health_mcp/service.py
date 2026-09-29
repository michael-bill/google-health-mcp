"""Transport-independent health queries, continuations and resumable exports."""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .catalog import (
    SHORT_ROLLUP,
    build_filter,
    data_type,
    record_id,
    validate_range,
)
from .errors import HealthError
from .google import Counters, Credentials, GoogleClient
from .store import Store, digest


@dataclass(repr=False)
class Caller:
    user_id: str
    credentials: Credentials
    counters: Counters


class HealthService:
    def __init__(self, settings, store: Store, google: GoogleClient):
        self.settings, self.store, self.google = settings, store, google
        self.export_locks = [asyncio.Lock() for _ in range(32)]
        self.semaphore = asyncio.Semaphore(8)

    async def upstream(
        self, caller, method, path, *, params=None, body=None, media=False, cache=True
    ):
        fingerprint = caller.credentials.fingerprint()
        key = digest(json.dumps([method, path, params, body, media], sort_keys=True))
        if cache:
            old = self.store.cache_get(caller.user_id, fingerprint, key)
            if old:
                caller.counters.cache_hits += 1
                return old[0], old[1], True
        async with self.semaphore:
            result = await self.google.request(
                caller.credentials,
                caller.counters,
                method,
                path,
                params=params,
                body=body,
                media=media,
            )
        now = time.time()
        if cache and self.settings.cache_seconds > 0:
            self.store.cache_put(
                caller.user_id, fingerprint, key, result, self.settings.cache_seconds
            )
        return result, now, False

    async def check_identity(self, caller: Caller) -> None:
        # Identity is checked upstream on every tool call. Revoked/changed credentials
        # cannot read old SQLite cache merely by retaining an MCP token.
        identity, _, _ = await self.upstream(caller, "GET", "users/me/identity", cache=False)
        account = identity.get("healthUserId")
        if not account:
            raise HealthError("GOOGLE_IDENTITY_MISSING")
        if not self.store.bind_account(caller.user_id, account):
            raise HealthError("GOOGLE_ACCOUNT_MISMATCH")

    async def account(self, caller: Caller, part: str) -> dict:
        if part not in ("profile", "settings", "identity", "pairedDevices"):
            raise HealthError("UNSUPPORTED_ACCOUNT_RESOURCE")
        result, fetched, cached = await self.upstream(caller, "GET", "users/me/" + part)
        return {"data": result, "fetched_at": self.iso(fetched), "cache_hit": cached}

    @staticmethod
    def iso(value: float) -> str:
        return datetime.fromtimestamp(value, UTC).isoformat()

    def query_spec(
        self,
        name,
        start=None,
        end=None,
        mode="auto",
        page_size=100,
        source=None,
        window_seconds=3600,
    ):
        row = data_type(name)
        a, _ = validate_range(start, end)
        if not 1 <= page_size <= 100:
            raise HealthError("PAGE_SIZE_MUST_BE_1_TO_100")
        if source not in (
            None,
            "all-sources",
            "google-wearables",
            "google-sources",
            "self-sources",
        ):
            raise HealthError("UNSUPPORTED_DATA_SOURCE")
        operation = mode
        if mode == "auto":
            operation = (
                "reconcile"
                if "reconcile" in row["operations"]
                else ("list" if "list" in row["operations"] else "dailyRollup")
            )
        if operation not in row["operations"] or operation not in (
            "list",
            "reconcile",
            "rollup",
            "dailyRollup",
        ):
            raise HealthError("OPERATION_NOT_SUPPORTED_FOR_DATA_TYPE")
        if operation == "list" and source and name in ("sleep", "food", "food-measurement-unit"):
            raise HealthError("SOURCE_FILTER_UNSUPPORTED_USE_RECONCILE_IF_AVAILABLE")
        spec = dict(
            name=name,
            start=start,
            end=end,
            operation=operation,
            page_size=min(page_size, 25) if name in ("sleep", "exercise") else page_size,
            source=source,
            page_token=None,
            window_seconds=window_seconds,
        )
        if operation in ("rollup", "dailyRollup"):
            if a is None:
                raise HealthError("ROLLUP_REQUIRES_RANGE")
            if operation == "dailyRollup" and type(a) is not date:
                raise HealthError("DAILY_ROLLUP_REQUIRES_DATE_BOUNDS")
            if operation == "rollup" and type(a) is not datetime:
                raise HealthError("ROLLUP_REQUIRES_TIMESTAMP_BOUNDS")
            max_seconds = (14 if name in SHORT_ROLLUP else 90) * 86400
            if operation == "rollup" and not 1 <= window_seconds <= max_seconds:
                raise HealthError("ROLLUP_WINDOW_OUT_OF_RANGE")
            spec["chunk_start"] = start
        else:
            spec["filter"] = build_filter(name, start, end)
        return spec

    @staticmethod
    def civil(d: date) -> dict:
        return {"date": {"year": d.year, "month": d.month, "day": d.day}, "time": {}}

    async def page(self, caller: Caller, spec: dict) -> dict:
        spec = dict(spec)
        name, operation = spec["name"], spec["operation"]
        path = f"users/me/dataTypes/{name}/dataPoints"
        params = {"pageSize": spec["page_size"]}
        if spec.get("page_token"):
            params["pageToken"] = spec["page_token"]
        if spec.get("source"):
            params["dataSourceFamily"] = "users/me/dataSourceFamilies/" + spec["source"]
        next_spec = None
        if operation in ("rollup", "dailyRollup"):
            a, end = validate_range(spec["chunk_start"], spec["end"])
            days = 14 if name in SHORT_ROLLUP else 90
            if operation == "dailyRollup":
                days = min(days, spec["page_size"])
            chunk = timedelta(days=days)
            if operation == "rollup":
                # Preserve aggregation boundaries when splitting long ranges.
                chunk = timedelta(
                    seconds=(days * 86400 // spec["window_seconds"]) * spec["window_seconds"]
                )
            b = min(end, a + chunk)
            body = dict(params)
            if operation == "dailyRollup":
                # Derived daily metrics can reject pageSize. Use date chunks
                # to bound the response instead of upstream page-size hints.
                body.pop("pageSize", None)
                body.update(
                    range={"start": self.civil(a), "end": self.civil(b)},
                    windowSizeDays=1,
                )
                suffix = ":dailyRollUp"
            else:
                body.update(
                    range={"startTime": a.isoformat(), "endTime": b.isoformat()},
                    windowSize=f"{spec['window_seconds']}s",
                )
                suffix = ":rollUp"
            result, fetched, cached = await self.upstream(caller, "POST", path + suffix, body=body)
            points = result.get("rollupDataPoints", [])
            if result.get("nextPageToken"):
                next_spec = {**spec, "page_token": result["nextPageToken"]}
            elif b < end:
                next_spec = {**spec, "chunk_start": b.isoformat(), "page_token": None}
        else:
            if spec.get("filter"):
                params["filter"] = spec["filter"]
            result, fetched, cached = await self.upstream(
                caller,
                "GET",
                path + (":reconcile" if operation == "reconcile" else ""),
                params=params,
            )
            points = result.get("dataPoints", [])
            if result.get("nextPageToken"):
                next_spec = {**spec, "page_token": result["nextPageToken"]}
        caller.counters.records += len(points)
        cursor = (
            self.store.cursor_put(caller.user_id, caller.credentials.fingerprint(), next_spec)
            if next_spec
            else None
        )
        response = {
            "data_type": name,
            "operation": operation,
            "range": {"start_inclusive": spec["start"], "end_exclusive": spec["end"]},
            "data": points,
            "count": len(points),
            "complete": cursor is None,
            "next_cursor": cursor,
            "fetched_at": self.iso(fetched),
            "cache_hit": cached,
            "notes": [
                "Dates use recorded civil time; sleep is selected by end time. Missing records are not zeros."
            ],
        }
        if len(json.dumps(response)) > 1_000_000:
            raise HealthError("RESULT_TOO_LARGE_REDUCE_PAGE_SIZE")
        return response

    async def query(self, caller: Caller, **kwargs) -> dict:
        return await self.page(caller, self.query_spec(**kwargs))

    async def next_page(self, caller: Caller, cursor: str) -> dict:
        spec = self.store.cursor_get(caller.user_id, caller.credentials.fingerprint(), cursor)
        if not spec:
            raise HealthError("CURSOR_INVALID_OR_EXPIRED")
        return await self.page(caller, spec)

    async def get_record(self, caller: Caller, name: str, point_id: str) -> dict:
        if "get" not in data_type(name)["operations"]:
            raise HealthError("GET_NOT_SUPPORTED")
        result, fetched, cached = await self.upstream(
            caller, "GET", f"users/me/dataTypes/{name}/dataPoints/{record_id(point_id)}"
        )
        caller.counters.records += 1
        return {"data": result, "fetched_at": self.iso(fetched), "cache_hit": cached}

    async def summary(self, caller: Caller, start: str, end: str) -> dict:
        validate_range(start, end)
        result = {
            "range": {"start_inclusive": start, "end_exclusive": end},
            "metrics": {},
        }
        for name in (
            "sleep",
            "steps",
            "daily-resting-heart-rate",
            "daily-heart-rate-variability",
            "daily-oxygen-saturation",
            "daily-respiratory-rate",
            "exercise",
        ):
            try:
                result["metrics"][name] = await self.query(
                    caller,
                    name=name,
                    start=start,
                    end=end,
                    mode="dailyRollup" if name == "steps" else "auto",
                    page_size=25,
                )
            except HealthError as e:
                result["metrics"][name] = {"error": e.as_dict(), "complete": False}
        result["complete"] = all(v.get("complete", False) for v in result["metrics"].values())
        result["notes"] = [
            "Per-metric continuation cursors retrieve remaining records. No medical reference ranges are inferred.",
            "An empty series means no returned records, not a healthy or zero measurement.",
        ]
        return result

    async def export(
        self,
        caller,
        name=None,
        start=None,
        end=None,
        mode="auto",
        export_id=None,
        max_pages=10,
    ):
        if not 1 <= max_pages <= 20:
            raise HealthError("MAX_PAGES_MUST_BE_1_TO_20")
        fingerprint = caller.credentials.fingerprint()
        if export_id is None:
            spec = self.query_spec(name, start, end, mode, page_size=25)
            cursor = self.store.cursor_put(caller.user_id, fingerprint, spec)
            export_id = self.store.export_create(caller.user_id, fingerprint, cursor)
        async with self.export_locks[int(digest(export_id)[:8], 16) % len(self.export_locks)]:
            row = self.store.export_get(caller.user_id, fingerprint, export_id)
            if not row:
                raise HealthError("EXPORT_NOT_FOUND")
            self.settings.export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.settings.export_dir / (export_id + ".jsonl")
            # SQLite count is the checkpoint; truncate an uncommitted tail after interruption.
            if path.exists():
                with path.open("r+b") as stream:
                    for _ in range(row["count"]):
                        if not stream.readline():
                            raise HealthError("EXPORT_FILE_INCOMPLETE")
                    stream.truncate()
            elif row["count"]:
                raise HealthError("EXPORT_FILE_MISSING")
            error = None
            for _ in range(max_pages):
                if not row["cursor"]:
                    break
                try:
                    page = await self.next_page(caller, row["cursor"])
                except HealthError as exc:
                    # Return the checkpoint even when the first batch fails, so
                    # callers can resume without losing the new export ID.
                    error = exc.as_dict()
                    break
                fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as stream:
                    for point in page["data"]:
                        stream.write(json.dumps(point, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                row["count"] += len(page["data"])
                row["cursor"] = page["next_cursor"]
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE exports SET count=?,cursor=?,complete=? WHERE id=?",
                        (
                            row["count"],
                            row["cursor"],
                            int(row["cursor"] is None),
                            export_id,
                        ),
                    )
            result = {
                "export_id": export_id,
                "records": row["count"],
                "complete": row["cursor"] is None,
                "next_action": None
                if row["cursor"] is None
                else "Call export_data with this export_id to continue",
                "download_path": "/exports/" + export_id,
            }
            if error:
                result["error"] = error
            return result

    async def route(self, caller: Caller, exercise_id: str) -> dict:
        result, fetched, _ = await self.upstream(
            caller,
            "GET",
            f"users/me/dataTypes/exercise/dataPoints/{record_id(exercise_id)}:exportExerciseTcx",
            params={"alt": "media"},
            media=True,
            cache=False,
        )
        fingerprint = caller.credentials.fingerprint()
        eid = self.store.export_create(caller.user_id, fingerprint, None)
        self.settings.export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.settings.export_dir / (eid + ".tcx")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result["tcx"])
        with self.store.connect() as db:
            db.execute("UPDATE exports SET complete=1,count=1 WHERE id=?", (eid,))
        return {
            "export_id": eid,
            "format": "tcx",
            "complete": True,
            "download_path": "/exports/" + eid,
            "fetched_at": self.iso(fetched),
        }

    def read_export(
        self, caller: Caller, export_id: str, offset: int = 0, max_bytes: int = 32000
    ) -> dict:
        """Read a bounded UTF-8 chunk of an owned export into model context."""
        if offset < 0 or not 1 <= max_bytes <= 64000:
            raise HealthError("INVALID_EXPORT_READ_BOUNDS")
        export = self.store.export_get(caller.user_id, caller.credentials.fingerprint(), export_id)
        if not export or not export["complete"]:
            raise HealthError("EXPORT_NOT_FOUND_OR_INCOMPLETE")
        for suffix in (".jsonl", ".tcx"):
            path = self.settings.export_dir / (export_id + suffix)
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(max_bytes)
                remaining = bool(stream.read(1))
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                if not remaining or exc.reason != "unexpected end of data" or exc.start == 0:
                    raise HealthError("EXPORT_OFFSET_NOT_UTF8_BOUNDARY") from None
                chunk = chunk[: exc.start]
                text = chunk.decode("utf-8")
                remaining = True
            return {
                "export_id": export_id,
                "text": text,
                "offset": offset,
                "next_offset": offset + len(chunk) if remaining else None,
                "complete": not remaining,
            }
        raise HealthError("EXPORT_FILE_MISSING")
