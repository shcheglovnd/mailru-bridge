"""Bridge configuration: how the local IMAP/SMTP servers listen and authenticate.

Clients authenticate to the *bridge* with a password you set here, never your Mail.ru password
(which the bridge does not store). The password is kept only as a salted PBKDF2 hash. Bind
addresses, ports, TLS certificate paths and the sync cadence live here too.

Stored at ``<state>/bridge.json`` (0600). Reach the bridge over your VPN; do not expose it to the
internet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import state_dir, write_private

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, dk_hex = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


@dataclass
class BridgeConfig:
    username: str = ""  # what the mail client logs in as (defaults to the account email)
    password_hash: str = ""
    bind_host: str = "127.0.0.1"
    imap_port: int = 1143
    smtp_port: int = 1025
    tls_cert: str = ""  # PEM; if set, IMAP/SMTP serve implicit TLS
    tls_key: str = ""
    poll_seconds: int = 180
    initial_depth: int = 2000  # newest messages to sync per folder on first run
    sync_window: int = 200  # newest messages re-checked each incremental poll
    flag_refresh_seconds: int = 900  # periodically recheck flags across all cached history
    # Shell command run (message appended as one argument) when the bridge cannot authenticate and a
    # `mailru-bridge login` is needed. Kept out of the code so the host owns any notifier secrets.
    alert_command: str = ""

    @staticmethod
    def path() -> Path:
        return state_dir() / "bridge.json"

    @classmethod
    def load(cls) -> "BridgeConfig | None":
        p = cls.path()
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        self.validate()
        write_private(self.path(), json.dumps(asdict(self), indent=2) + "\n")

    def validate(self) -> None:
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("TLS requires both a certificate and a private key")
        for port in (self.imap_port, self.smtp_port):
            if not 1 <= port <= 65535:
                raise ValueError("ports must be between 1 and 65535")
        if self.imap_port == self.smtp_port:
            raise ValueError("IMAP and SMTP must use different ports")
        if min(self.poll_seconds, self.initial_depth, self.sync_window, self.flag_refresh_seconds) <= 0:
            raise ValueError("poll interval and sync depths must be positive")

    @property
    def tls(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    def check_login(self, username: str, password: str) -> bool:
        if not self.password_hash:
            return False
        user_ok = hmac.compare_digest(username.lower().encode("utf-8"), self.username.lower().encode("utf-8"))
        return user_ok and verify_password(password, self.password_hash)
