"""Exercise a running MCP without printing credentials or medical values.

Run manually after starting the server. This is deliberately excluded from the
offline test suite. It consumes real API quota and writes normal usage events.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from health_mcp.catalog import CATALOG


async def main():
    load_dotenv(".env", override=False)
    load_dotenv(".secrets/client.env", override=False)
    headers = {
        "Authorization": "Bearer " + os.environ["HEALTH_MCP_TOKEN"],
        "X-Google-Refresh-Token": os.environ["GOOGLE_REFRESH_TOKEN"],
    }
    url = os.getenv("VERIFY_MCP_URL", "http://127.0.0.1:8767/mcp")
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    week_ago = tomorrow - timedelta(days=7)
    failures = 0
    async with httpx.AsyncClient(headers=headers, timeout=120, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(json.dumps({"tools_discovered": len(tools.tools)}), flush=True)
                for tool in ("get_profile", "get_settings", "get_devices", "get_data_status"):
                    result = await session.call_tool(tool, {})
                    print(json.dumps({"tool": tool, "ok": not result.isError}), flush=True)
                    failures += int(result.isError)
                for name, definition in CATALOG.items():
                    arguments = {"data_type": name, "page_size": 2}
                    if definition["kind"] != "Food":
                        arguments.update(start=week_ago.isoformat(), end=tomorrow.isoformat())
                    result = await session.call_tool("query_data", arguments)
                    summary = {"data_type": name, "ok": not result.isError}
                    if result.isError:
                        # Server errors are fixed codes; never print untrusted response text.
                        summary["error"] = "MCP_TOOL_ERROR"
                        failures += 1
                    else:
                        payload = result.structuredContent
                        if not payload:
                            payload = json.loads(result.content[0].text)
                        summary.update(
                            records=payload.get("count"), has_more=not payload.get("complete", True)
                        )
                    print(json.dumps(summary), flush=True)
                result = await session.call_tool(
                    "get_health_summary",
                    {"start": week_ago.isoformat(), "end": tomorrow.isoformat()},
                )
                payload = result.structuredContent or {}
                print(
                    json.dumps(
                        {
                            "tool": "get_health_summary",
                            "ok": not result.isError,
                            "metric_errors": sum(
                                "error" in item for item in payload.get("metrics", {}).values()
                            ),
                        }
                    ),
                    flush=True,
                )
    return min(failures, 1)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        print(
            "Live verification failed; no credentials or upstream payloads were printed", flush=True
        )
        raise SystemExit(1) from None
