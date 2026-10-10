"""The Android phone the bridge pretends to be.

The server sees one consistent device per account: the profile is generated once
(random DeviceID, fixed model) and stored with the account, so every login, token
refresh and API call looks like the same phone running the official app.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field

# Official app release whose protocol this bridge follows (ru.mail.mailapp).
APP_PACKAGE = "ru.mail.mailapp"
APP_VERSION = "15.108.0.148470"
APP_BUILD = "148470"
USER_AGENT = f"mobmail android {APP_VERSION} {APP_PACKAGE}"

# Value of the app's R.string.auth_csrf_header, sent as X-Mobile-App on login requests.
X_MOBILE_APP = "e552fda2e6c711eaadc10242ac120002"


def _local_gmt_offset() -> str:
    """The phone's timezone the way the app formats it (SimpleDateFormat "ZZZZ")."""
    offset = -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone)
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset) // 60, 60)
    return f"GMT{sign}{hours:02d}:{minutes:02d}"


@dataclass
class Device:
    # The app sends an MD5-derived id: 32 lowercase hex characters.
    device_id: str = field(default_factory=lambda: secrets.token_hex(16))
    vendor: str = "Google"
    model: str = "Pixel 8"
    os_version: str = "14"
    build_id: str = "AP2A.240905.003"
    webview_chrome: str = "129.0.6668.100"
    playservices: str = "243731037"
    language: str = "ru_RU"
    country: str = "RU"
    timezone: str = field(default_factory=_local_gmt_offset)

    @property
    def device_name(self) -> str:
        return self.model if self.model.startswith(self.vendor) else f"{self.vendor} {self.model}"

    @property
    def webview_user_agent(self) -> str:
        return (
            f"Mozilla/5.0 (Linux; Android {self.os_version}; {self.model} Build/{self.build_id}; wv) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/{self.webview_chrome} "
            "Mobile Safari/537.36"
        )

    def platform_params(self) -> list[tuple[str, str]]:
        """Query parameters the app appends to every request (PlatformParams + DeviceInfoProvider)."""
        return [
            ("mp", "android"),
            ("mmp", "mail"),
            ("client", "mobile"),
            ("DeviceID", self.device_id),
            ("playservices", self.playservices),
            ("os", "Android"),
            ("os_version", self.os_version),
            ("ver", APP_VERSION),
            ("appbuild", APP_BUILD),
            ("vendor", self.vendor),
            ("model", self.model),
            ("device_type", "Smartphone"),
            ("country", self.country),
            ("language", self.language),
            ("Lang", self.language),
            ("timezone", self.timezone),
            ("device_name", self.device_name),
        ]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Device:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)
