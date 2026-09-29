"""Load private configuration without exposing credential values."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database: Path = Path("data/health.sqlite3")
    export_dir: Path = Path("data/exports")
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 8767
    cache_seconds: int = 300
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:*", "http://localhost:*")

    @classmethod
    def load(cls, env_file: str = ".env"):
        # The program consumes credentials; it never prints their values.
        load_dotenv(env_file, override=False)
        client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        path = os.getenv("GOOGLE_CLIENT_SECRETS_FILE")
        if path and not (client_id and secret):
            try:
                doc = json.loads(Path(path).expanduser().read_text())
                client = doc.get("web") or doc.get("installed") or {}
                client_id, secret = client["client_id"], client["client_secret"]
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError(
                    "Cannot load OAuth client credentials; check the private file locally"
                ) from None
        return cls(
            database=Path(os.getenv("SQLITE_PATH", "data/health.sqlite3")).expanduser(),
            export_dir=Path(os.getenv("EXPORT_DIR", "data/exports")).expanduser(),
            client_id=client_id,
            client_secret=secret,
            refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN", ""),
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8767")),
            cache_seconds=int(os.getenv("CACHE_TTL_SECONDS", "300")),
            allowed_hosts=tuple(
                os.getenv("MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*").split(",")
            ),
            allowed_origins=tuple(
                os.getenv("MCP_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*").split(",")
            ),
        )
