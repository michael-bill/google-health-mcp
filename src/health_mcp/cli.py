"""Operator commands and private credential handoff to MCP clients."""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .errors import HealthError
from .google import Counters, Credentials, GoogleClient
from .service import Caller, HealthService
from .store import Store


def private_write(path: str | Path, content: str) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


async def smoke(settings: Settings, store: Store, token: str) -> None:
    uid = store.authenticate(token)
    if uid is None:
        raise HealthError("MCP_AUTHENTICATION_REQUIRED")
    google = GoogleClient()
    caller = Caller(
        uid,
        Credentials(settings.client_id, settings.client_secret, settings.refresh_token),
        Counters(),
    )
    service = HealthService(settings, store, google)
    try:
        await service.check_identity(caller)
        for name in ("profile", "settings", "pairedDevices"):
            try:
                result = await service.account(caller, name)
                print(json.dumps({"check": name, "status": "ok", "fields": len(result["data"])}))
            except HealthError as e:
                print(json.dumps({"check": name, "error": e.as_dict()}))
        for name in ("sleep", "steps", "daily-resting-heart-rate", "nutrition-log"):
            try:
                result = await service.query(caller, name=name, page_size=2)
                print(
                    json.dumps(
                        {
                            "check": name,
                            "status": "ok",
                            "records": result["count"],
                            "has_more": not result["complete"],
                        }
                    )
                )
            except HealthError as e:
                print(json.dumps({"check": name, "error": e.as_dict()}))
    finally:
        await google.close()


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Private Google Health MCP")
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--transport", choices=["http", "stdio"], default="http")
    serve.add_argument("--credentials-file", default=".secrets/client.env")
    add = sub.add_parser("add-user")
    add.add_argument("label")
    add.add_argument("--credentials-file", required=True)
    revoke = sub.add_parser("revoke-user")
    revoke.add_argument("label")
    stats = sub.add_parser("stats")
    stats.add_argument("--days", type=int, default=30)
    sub.add_parser("prune")
    headers = sub.add_parser("headers")
    headers.add_argument("--credentials-file", required=True)
    test = sub.add_parser("smoke")
    test.add_argument("--credentials-file", default=".secrets/client.env")
    args = parser.parse_args()
    # Client-side header helper is invoked by Codex, never as an agent-visible tool.
    if args.command == "headers":
        load_dotenv(args.env_file, override=False)
        load_dotenv(args.credentials_file, override=True)
        token = os.getenv("HEALTH_MCP_TOKEN")
        refresh = os.getenv("GOOGLE_REFRESH_TOKEN")
        if not token or not refresh:
            print("Required credentials are unavailable", file=sys.stderr)
            return 1
        result = {"Authorization": "Bearer " + token, "X-Google-Refresh-Token": refresh}
        if os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"):
            result.update(
                {
                    "X-Google-Client-Id": os.environ["GOOGLE_CLIENT_ID"],
                    "X-Google-Client-Secret": os.environ["GOOGLE_CLIENT_SECRET"],
                }
            )
        print(json.dumps(result))
        return 0
    try:
        if args.command == "smoke" or (args.command == "serve" and args.transport == "stdio"):
            # Load explicit per-user credentials before Settings snapshots env.
            # A blank refresh-token placeholder in server .env must not hide it.
            load_dotenv(args.credentials_file, override=True)
        settings = Settings.load(args.env_file)
        store = Store(settings.database)
        if args.command == "add-user":
            target = Path(args.credentials_file)
            if target.exists():
                raise HealthError("CREDENTIAL_FILE_ALREADY_EXISTS")
            uid, token = store.add_user(args.label)
            try:
                private_write(
                    target,
                    "HEALTH_MCP_TOKEN="
                    + token
                    + "\n# Set GOOGLE_REFRESH_TOKEN locally or provide it through the environment.\n",
                )
            except OSError:
                store.disable_user(args.label)
                raise HealthError("CREDENTIAL_FILE_WRITE_FAILED") from None
            print(
                json.dumps(
                    {
                        "user_id": uid,
                        "credential_file_created": True,
                        "token_printed": False,
                    }
                )
            )
        elif args.command == "revoke-user":
            print(json.dumps({"revoked": bool(store.disable_user(args.label))}))
        elif args.command == "stats":
            if not 1 <= args.days <= 3650:
                raise HealthError("INVALID_STATS_PERIOD")
            print(json.dumps(store.stats(args.days), indent=2))
        elif args.command == "prune":
            store.prune()
            print("Expired cache, cursors and usage older than 90 days removed")
        elif args.command == "smoke":
            load_dotenv(args.credentials_file, override=False)
            asyncio.run(smoke(settings, store, os.getenv("HEALTH_MCP_TOKEN", "")))
        elif args.command == "serve":
            from .server import Runtime, make_app, make_server

            logging.basicConfig(level=logging.CRITICAL)
            usage = logging.getLogger("health_mcp.usage")
            usage.setLevel(logging.INFO)
            usage.propagate = False
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            usage.addHandler(handler)
            store.prune()
            local = None
            if args.transport == "stdio":
                load_dotenv(args.credentials_file, override=False)
                uid = store.authenticate(os.getenv("HEALTH_MCP_TOKEN", ""))
                if not uid:
                    raise HealthError("MCP_AUTHENTICATION_REQUIRED")
                local = (
                    uid,
                    Credentials(
                        settings.client_id,
                        settings.client_secret,
                        settings.refresh_token,
                    ),
                )
            runtime = Runtime(settings, store=store, local_caller=local)
            if args.transport == "stdio":
                make_server(runtime).run(transport="stdio")
            else:
                import uvicorn

                uvicorn.run(
                    make_app(runtime),
                    host=settings.host,
                    port=settings.port,
                    log_level="critical",
                    access_log=False,
                )
        return 0
    except HealthError as e:
        print(json.dumps({"error": e.code, "http_status": e.status}), file=sys.stderr)
        return 1
    except Exception:
        # Never print credential-bearing locals, HTTP response bodies or exceptions.
        print(
            "Operation failed; check configuration and private file permissions locally",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
