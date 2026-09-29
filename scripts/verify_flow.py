"""Opt-in live checks for private header injection, pagination and exports.

No credentials, response bodies, record IDs or health values are printed.
"""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> int:
    helper = subprocess.run(
        [
            sys.executable,
            "-m",
            "health_mcp",
            "headers",
            "--credentials-file",
            ".secrets/client.env",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if helper.returncode:
        print("Header helper failed; output withheld")
        return 1
    headers = json.loads(helper.stdout)
    today = datetime.now(UTC).date()
    failures = 0
    async with httpx.AsyncClient(headers=headers, timeout=180, trust_env=False) as http:
        async with streamable_http_client("http://127.0.0.1:8767/mcp", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool, arguments):
                    nonlocal failures
                    result = await session.call_tool(tool, arguments)
                    if result.isError:
                        failures += 1
                        print(json.dumps({"check": tool, "ok": False}), flush=True)
                        return None
                    return result.structuredContent or json.loads(result.content[0].text)

                profile = await call("get_profile", {})
                print(
                    json.dumps({"check": "private_header_helper", "ok": profile is not None}),
                    flush=True,
                )
                arguments = {
                    "data_type": "total-calories",
                    "start": (today - timedelta(days=20)).isoformat(),
                    "end": today.isoformat(),
                    "page_size": 5,
                }
                page = await call("query_data", arguments)
                page_count = 0
                while page:
                    page_count += 1
                    if page["complete"]:
                        break
                    if page_count > 20:
                        raise RuntimeError("Continuation did not terminate")
                    page = await call("next_page", {"cursor": page["next_cursor"]})
                print(
                    json.dumps(
                        {
                            "check": "daily_rollup_continuation",
                            "pages": page_count,
                            "complete": bool(page and page["complete"]),
                        }
                    ),
                    flush=True,
                )

                physical = await call(
                    "query_data",
                    {
                        "data_type": "heart-rate",
                        "mode": "rollup",
                        "start": (today - timedelta(days=1)).isoformat() + "T00:00:00Z",
                        "end": today.isoformat() + "T00:00:00Z",
                        "window_seconds": 3600,
                        "page_size": 2,
                    },
                )
                print(
                    json.dumps({"check": "physical_rollup", "ok": physical is not None}), flush=True
                )

                export = await call(
                    "export_data",
                    {
                        "data_type": "daily-resting-heart-rate",
                        "start": (today - timedelta(days=7)).isoformat(),
                        "end": today.isoformat(),
                        "max_pages": 2,
                    },
                )
                if export and export["complete"]:
                    response = await http.get("http://127.0.0.1:8767" + export["download_path"])
                    valid = response.status_code == 200 and all(
                        isinstance(json.loads(line), dict) for line in response.text.splitlines()
                    )
                    print(
                        json.dumps(
                            {"check": "jsonl_download", "ok": valid, "records": export["records"]}
                        ),
                        flush=True,
                    )
                    failures += int(not valid)
                    chunk = await call(
                        "read_export", {"export_id": export["export_id"], "max_bytes": 100}
                    )
                    print(json.dumps({"check": "export_read", "ok": chunk is not None}), flush=True)

                workouts = await call(
                    "query_data", {"data_type": "exercise", "mode": "list", "page_size": 1}
                )
                if workouts and workouts["data"]:
                    point_id = workouts["data"][0]["name"].rsplit("/", 1)[-1]
                    record = await call(
                        "get_record", {"data_type": "exercise", "record_id": point_id}
                    )
                    print(
                        json.dumps({"check": "individual_record", "ok": record is not None}),
                        flush=True,
                    )
                    route = await call("export_workout_route", {"exercise_id": point_id})
                    if route:
                        response = await http.get("http://127.0.0.1:8767" + route["download_path"])
                        valid = response.status_code == 200
                        if valid:
                            ElementTree.fromstring(response.content)
                        print(json.dumps({"check": "tcx_export", "ok": valid}), flush=True)
                        failures += int(not valid)
                else:
                    print(
                        json.dumps({"check": "workout_export", "skipped": "no_record"}), flush=True
                    )
    return min(failures, 1)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        print("Live flow check failed; details withheld to protect credentials")
        raise SystemExit(1) from None
