"""Local state: tokens and the device profile, kept outside the repository with 0600 permissions.

Default location is ``$XDG_STATE_HOME/mailru-bridge`` (``~/.local/state/mailru-bridge``);
override with ``MAILRU_BRIDGE_STATE``. The password is never stored.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .device import Device


def state_dir() -> Path:
    base = os.environ.get("MAILRU_BRIDGE_STATE")
    if not base:
        xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        base = os.path.join(xdg, "mailru-bridge")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, readable by the owner only."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


@dataclass
class Account:
    email: str
    device: Device = field(default_factory=Device)
    refresh_token: str = ""
    access_token: str = ""
    access_expires_at: float = 0.0
    # "Trusted device" cookie from the SMS step; lets a later password login skip the SMS.
    tsa_cookie: str = ""

    @property
    def access_token_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.access_expires_at - 60

    @staticmethod
    def path() -> Path:
        return state_dir() / "account.json"

    @classmethod
    def load(cls) -> Account | None:
        path = cls.path()
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        data["device"] = Device.from_dict(data.get("device", {}))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        data = asdict(self)
        data["device"] = self.device.to_dict()
        write_private(self.path(), json.dumps(data, indent=2) + "\n")
