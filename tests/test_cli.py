import json
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_header_helper_loads_client_file_over_blank_server_placeholder(tmp_path):
    server_env = tmp_path / "server.env"
    client_env = tmp_path / "client.env"
    server_env.write_text("GOOGLE_REFRESH_TOKEN=\n")
    client_env.write_text("HEALTH_MCP_TOKEN=fake-mcp\nGOOGLE_REFRESH_TOKEN=fake-refresh\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GOOGLE_", "HEALTH_MCP_"))
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "health_mcp",
            "--env-file",
            str(server_env),
            "headers",
            "--credentials-file",
            str(client_env),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "Authorization": "Bearer fake-mcp",
        "X-Google-Refresh-Token": "fake-refresh",
    }
    assert not result.stderr


async def test_stdio_client_discovers_tools(settings, store, tmp_path):
    _, token = store.add_user("stdio", "stdio-key")
    server_env = tmp_path / "server.env"
    client_env = tmp_path / "client.env"
    server_env.write_text(f"SQLITE_PATH={settings.database}\nGOOGLE_REFRESH_TOKEN=\n")
    client_env.write_text(f"HEALTH_MCP_TOKEN={token}\nGOOGLE_REFRESH_TOKEN=fake-refresh\n")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "health_mcp",
            "--env-file",
            str(server_env),
            "serve",
            "--transport",
            "stdio",
            "--credentials-file",
            str(client_env),
        ],
        env={"GOOGLE_CLIENT_ID": "fake-client", "GOOGLE_CLIENT_SECRET": "fake-secret"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 14
            result = await session.call_tool("list_data_types", {})
            assert not result.isError
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert len(payload["types"]) == 37
