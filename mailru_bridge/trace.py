"""Redacted HTTP trace for reverse engineering and bug reports.

Each run that is started with ``--trace`` gets a directory under ``<state>/trace``. It holds
``trace.jsonl`` (one line per request and response) and the raw HTML pages. Passwords,
tokens and cookie values are masked, but pages can still contain your address and
one-time CSRF values, so traces stay private: the directory is created 0700 and is
never written inside the repository.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

import httpx

from .store import state_dir

SENSITIVE = {
    "password", "access_token", "refresh_token", "token", "tsa", "mpop", "csrf", "code",
    "authcode", "bind_token", "ludwigtoken", "ludwig_token", "sdcs", "ssdc", "act",
    "md5_signature", "md5_post_signature", "otp", "smscode", "session", "sid", "x-mobile-app",
}


def mask(value: str) -> str:
    return f"<{len(value)} chars>" if value else ""


def redact_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, mask(v) if k.lower() in SENSITIVE else v) for k, v in pairs]


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(redact_pairs(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(parts._replace(query=query))


def redact_json(obj):
    if isinstance(obj, dict):
        return {
            k: (mask(v) if isinstance(v, str) and k.lower() in SENSITIVE else redact_json(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    return obj


def redact_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    out = []
    for name, value in headers.multi_items():
        low = name.lower()
        if low == "cookie":
            value = "; ".join(f"{c.split('=', 1)[0].strip()}={mask(c.split('=', 1)[-1])}" for c in value.split(";"))
        elif low == "set-cookie":
            cookie, _, attrs = value.partition(";")
            cname, _, cval = cookie.partition("=")
            value = f"{cname}={mask(cval)};{attrs}"
        elif low in SENSITIVE or low == "authorization":
            value = mask(value)
        out.append((name, value))
    return out


class Trace:
    def __init__(self, name: str):
        root = state_dir() / "trace"
        root.mkdir(exist_ok=True)
        root.chmod(0o700)
        self.dir: Path = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
        self.dir.mkdir(mode=0o700)
        self._log = (self.dir / "trace.jsonl").open("a")
        self._seq = 0

    def _write(self, record: dict) -> None:
        record["t"] = round(time.time(), 3)
        self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log.flush()

    def note(self, message: str, **fields) -> None:
        self._write({"note": message, **redact_json(fields)})

    def on_request(self, request: httpx.Request) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "request": f"{request.method} {redact_url(str(request.url))}",
            "headers": redact_headers(request.headers),
        }
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded"):
            record["form"] = redact_pairs(parse_qsl(request.content.decode(), keep_blank_values=True))
        elif request.content:
            record["body_bytes"] = len(request.content)
        self._write(record)

    def on_response(self, response: httpx.Response) -> None:
        response.read()
        record = {
            "seq": self._seq,
            "status": response.status_code,
            "url": redact_url(str(response.request.url)),
            "headers": redact_headers(response.headers),
        }
        ctype = response.headers.get("content-type", "")
        text = response.text
        try:
            record["json"] = redact_json(json.loads(text))
        except ValueError:
            if text:
                ext = "html" if "html" in ctype else "txt"
                page = self.dir / f"{self._seq:02d}-response.{ext}"
                page.write_text(text)
                page.chmod(0o600)
                record["body_file"] = page.name
                record["body_bytes"] = len(response.content)
        self._write(record)

    def hooks(self) -> dict:
        return {"request": [self.on_request], "response": [self.on_response]}

    def close(self) -> None:
        self._log.close()
