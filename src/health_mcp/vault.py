"""Versioned AES-GCM encryption; the key ring lives outside the database."""

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import HealthError


class TokenVault:
    def __init__(self, key_file: Path):
        try:
            ring = json.loads(key_file.read_text())
            self.active = ring["active"]
            self.keys = {
                version: base64.b64decode(value, validate=True)
                for version, value in ring["keys"].items()
            }
            if self.active not in self.keys or any(len(key) != 32 for key in self.keys.values()):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise HealthError("TOKEN_KEY_FILE_INVALID") from None

    @staticmethod
    def create_key_ring() -> str:
        return json.dumps(
            {"active": "v1", "keys": {"v1": base64.b64encode(os.urandom(32)).decode()}}
        )

    def seal(self, value: str, context: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.keys[self.active]).encrypt(nonce, value.encode(), context.encode())
        return json.dumps(
            {
                "version": self.active,
                "payload": base64.b64encode(nonce + ciphertext).decode(),
            }
        )

    def open(self, envelope: str, context: str) -> str:
        try:
            record = json.loads(envelope)
            payload = base64.b64decode(record["payload"], validate=True)
            return (
                AESGCM(self.keys[record["version"]])
                .decrypt(payload[:12], payload[12:], context.encode())
                .decode()
            )
        except Exception:
            raise HealthError("TOKEN_DECRYPTION_FAILED") from None
