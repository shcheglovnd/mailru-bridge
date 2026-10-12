"""Async client for the Mail.ru mail API, as used by the official Android app.

Every call is an app-style request to ``aj-https.mail.ru``: the command's own parameters, the
account (``email``, ``act_mode``), the pinned device's platform parameters and the OAuth
``access_token`` go into the URL query, which is signed with ``md5_signature``. ``POST`` bodies are
form-encoded and signed separately with ``md5_post_signature`` over the body fields only
(``ServerCommandBase.onPrepareRequestBody`` signs with ``Uri.EMPTY``).

Responses are JSON envelopes ``{"status": <code>, "body": …, "last_modified": …}`` — the HTTP
status is usually 200 and the real outcome is ``status``. An expired access token answers
``{"status": 403, "body": "user"}``; the client then refreshes it once and retries.

All traffic goes through one :class:`RateLimiter` so that the bridge never bursts: a calm, single
"phone" is what keeps anti-fraud quiet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from .auth import CLIENT_ID, OAUTH_HOST, Authenticator, apply_tokens
from .device import USER_AGENT
from .sign import signature, signed_query
from .store import Account

log = logging.getLogger(__name__)

API_HOST = "aj-https.mail.ru"

# Fixed ids of the system folders (identical for every account).
INBOX = "0"
SENT = "500000"
DRAFTS = "500001"
TRASH = "500002"
SPAM = "950"
ARCHIVE = "500010"
SOCIAL = "500011"

# The app asks for the newest messages first.
SORT_NEWEST_FIRST = '{"type":"id", "order":"desc"}'


class ApiError(Exception):
    """The API answered with an error status."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthExpired(ApiError):
    """The session is gone (refresh failed); the user must run ``mailru-bridge login``."""


class RateLimited(ApiError):
    """The server asked us to slow down."""


class NotFound(ApiError):
    """The message or folder does not exist (any more)."""


# -- data -------------------------------------------------------------------------------------


@dataclass
class RemoteFolder:
    id: str
    name: str
    parent: str = "-1"
    type: str = "user"
    system: bool = False
    total: int = 0
    unread: int = 0
    flagged: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, data: dict) -> RemoteFolder:
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or ""),
            parent=str(data.get("parent", "-1")),
            type=str(data.get("type") or "user"),
            system=bool(data.get("system")),
            total=int(data.get("messages_total") or 0),
            unread=int(data.get("messages_unread") or 0),
            flagged=int(data.get("messages_flagged") or 0),
            raw=data,
        )


def _addresses(items: Any) -> list[tuple[str, str]]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append((str(item.get("name") or ""), str(item.get("email") or "")))
    return out


@dataclass
class RemoteMessage:
    id: str
    folder: str
    date: int
    size: int = 0
    unread: bool = False
    flagged: bool = False
    answered: bool = False
    forwarded: bool = False
    has_attachments: bool = False
    subject: str = ""
    sender: list[tuple[str, str]] = field(default_factory=list)
    to: list[tuple[str, str]] = field(default_factory=list)
    cc: list[tuple[str, str]] = field(default_factory=list)
    thread_id: str = ""

    @classmethod
    def from_json(cls, data: dict) -> RemoteMessage:
        flags = data.get("flags") or {}
        corr = data.get("correspondents") or {}
        return cls(
            id=str(data["id"]),
            folder=str(data.get("folder", "")),
            date=int(data.get("date") or 0),
            size=int(data.get("size") or 0),
            unread=bool(flags.get("unread")),
            flagged=bool(flags.get("flagged")),
            answered=bool(flags.get("reply")),
            forwarded=bool(flags.get("forward")),
            has_attachments=bool(flags.get("attach")),
            subject=str(data.get("subject") or ""),
            sender=_addresses(corr.get("from")),
            to=_addresses(corr.get("to")),
            cc=_addresses(corr.get("cc")),
            thread_id=str(data.get("m_thread_id") or data.get("thread_id") or ""),
        )


@dataclass
class MessagePage:
    messages: list[RemoteMessage]
    total: int
    last_modified: int


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"
    content_id: str | None = None  # set for inline parts referenced as cid: from the HTML


@dataclass
class OutgoingMessage:
    sender: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    text: str | None = None
    html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    reply_to_id: str | None = None  # Mail.ru id of the message this answers
    forward_of_id: str | None = None
    priority: int = 3
    receipt: bool = False


# -- rate limiting ----------------------------------------------------------------------------


class RateLimiter:
    """Spaces requests out: at least ``interval`` seconds between starts, plus a little jitter.

    ``penalize`` stretches the spacing after a 429 or a server error and decays back slowly.
    """

    def __init__(self, interval: float = 0.15, jitter: float = 0.1):
        self.interval = interval
        self.jitter = jitter
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._penalty = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            spacing = self.interval + random.uniform(0, self.jitter) + self._penalty
            self._next = now + spacing
            self._penalty *= 0.8
            if self._penalty < 0.05:
                self._penalty = 0.0

    def penalize(self, seconds: float) -> None:
        self._penalty = min(max(self._penalty, seconds), 300.0)


# -- client -----------------------------------------------------------------------------------


class MailruApi:
    def __init__(
        self,
        account: Account,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        host: str = API_HOST,
    ):
        self.account = account
        self.client = client or make_async_client()
        self.limiter = limiter or RateLimiter()
        self.host = host
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- session ---------------------------------------------------------------------------

    async def access_token(self) -> str:
        if self.account.access_token_valid:
            return self.account.access_token
        return await self.refresh_token()

    async def refresh_token(self, stale: str | None = None) -> str:
        """Refresh the access token. ``stale`` is the token the server just rejected."""
        async with self._refresh_lock:
            # Another task may have refreshed while we waited for the lock.
            if self.account.access_token_valid and self.account.access_token != stale:
                return self.account.access_token
            if not self.account.refresh_token:
                raise AuthExpired("No refresh token; run `mailru-bridge login`.")
            url = httpx.URL(
                f"https://{OAUTH_HOST}/token",
                params=signed_query(self.account.device.platform_params()),
            )
            form = {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.account.refresh_token,
            }
            await self.limiter.wait()
            try:
                resp = await self.client.post(url, data=form, headers={"User-Agent": USER_AGENT})
            except httpx.HTTPError as exc:
                raise ApiError(f"Token refresh failed: {exc}") from exc
            try:
                body = resp.json()
            except ValueError:
                body = {}
            container = Authenticator._token_container(body if isinstance(body, dict) else {})
            if resp.status_code != 200 or container is None:
                raise AuthExpired(
                    f"Token refresh failed (HTTP {resp.status_code}); run `mailru-bridge login`.",
                    resp.status_code,
                    body,
                )
            from .auth import Tokens

            apply_tokens(
                self.account,
                Tokens(
                    container["access_token"],
                    container.get("refresh_token", self.account.refresh_token),
                    container.get("expires_in"),
                ),
            )
            self.account.save()
            log.info("access token refreshed")
            return self.account.access_token

    # -- request plumbing --------------------------------------------------------------------

    def _url(self, path: str, query: list[tuple[str, str]], token: str) -> httpx.URL:
        params = [
            *query,
            ("email", self.account.email),
            ("act_mode", "act"),
            *self.account.device.platform_params(),
            ("access_token", token),
        ]
        return httpx.URL(f"https://{self.host}/{path}", params=signed_query(params))

    async def _call(
        self,
        path: str,
        query: list[tuple[str, str]] | None = None,
        form: list[tuple[str, str]] | None = None,
        files: dict | None = None,
        binary: bool = False,
        retries: int = 3,
    ) -> Any:
        """Perform one API call; return the JSON envelope (or raw bytes for ``binary``)."""
        query = list(query or [])
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            token = await self.access_token()
            url = self._url(path, query, token)
            headers = {"User-Agent": USER_AGENT}
            await self.limiter.wait()
            try:
                if files is not None:
                    # Multipart uploads carry no md5_post_signature (UploadFileCommand
                    # overrides onPrepareRequestBody without signing).
                    resp = await self.client.post(url, data=dict(form or []), files=files, headers=headers)
                elif form is not None:
                    pairs = [*form, ("md5_post_signature", signature(form))]
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    resp = await self.client.post(url, content=urlencode(pairs).encode(), headers=headers)
                else:
                    resp = await self.client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt > retries:
                    raise ApiError(f"{path}: network error: {exc}") from exc
                await asyncio.sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 429:
                self.limiter.penalize(30.0)
                raise RateLimited(f"{path}: HTTP 429", 429)
            if resp.status_code >= 500:
                self.limiter.penalize(5.0)
                if attempt > retries:
                    raise ApiError(f"{path}: HTTP {resp.status_code}", resp.status_code)
                await asyncio.sleep(min(2**attempt, 30))
                continue

            ctype = resp.headers.get("content-type", "")
            if binary and "json" not in ctype and resp.status_code == 200:
                return resp.content

            try:
                envelope = resp.json()
            except ValueError:
                raise ApiError(
                    f"{path}: unexpected response (HTTP {resp.status_code}, {ctype})",
                    resp.status_code,
                    resp.text[:200],
                ) from None
            if not isinstance(envelope, dict):
                raise ApiError(f"{path}: unexpected JSON", resp.status_code, envelope)

            status = envelope.get("status", resp.status_code)
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = resp.status_code
            body = envelope.get("body")

            if status == 200:
                return envelope
            if status == 403 and body == "user":
                if refreshed:
                    raise AuthExpired(f"{path}: session rejected after refresh", status, body)
                refreshed = True
                await self.refresh_token(stale=token)
                continue
            if status == 429:
                self.limiter.penalize(30.0)
                raise RateLimited(f"{path}: status 429", status, body)
            if status >= 500:
                self.limiter.penalize(5.0)
                if attempt > retries:
                    raise ApiError(f"{path}: status {status}", status, body)
                await asyncio.sleep(min(2**attempt, 30))
                continue
            if status == 404 or _is_not_found(body):
                raise NotFound(f"{path}: not found ({json.dumps(body, ensure_ascii=False)[:200]})", status, body)
            raise ApiError(
                f"{path}: status {status} ({json.dumps(body, ensure_ascii=False)[:300]})", status, body
            )

    # -- reading ---------------------------------------------------------------------------

    async def folders(self) -> list[RemoteFolder]:
        body = (await self._call("api/v1/folders")).get("body")
        return [RemoteFolder.from_json(f) for f in body or [] if isinstance(f, dict)]

    async def messages(
        self, folder: str, offset: int = 0, limit: int = 100, last_modified: int = 0
    ) -> MessagePage:
        """One page of a folder, newest first (``GET api/v1/messages/status``)."""
        query = [
            ("sort", SORT_NEWEST_FIRST),
            ("limit", str(limit)),
            ("folder", str(folder)),
            ("offset", str(offset)),
            ("last_modified", str(last_modified if offset == 0 else 1)),
            ("snippet_limit", "183"),
        ]
        envelope = await self._call("api/v1/messages/status", query)
        body = envelope.get("body")
        if not isinstance(body, dict):
            raise ApiError("messages/status: unexpected body", body=body)
        msgs = [RemoteMessage.from_json(m) for m in body.get("messages") or [] if isinstance(m, dict)]
        return MessagePage(
            messages=msgs,
            total=int(body.get("messages_total") or 0),
            last_modified=int(envelope.get("last_modified") or 0),
        )

    async def download(self, message_id: str, folder: str) -> bytes:
        """The message's original RFC 822 source (``POST api/v1/messages/message/download``)."""
        data = await self._call(
            "api/v1/messages/message/download",
            form=[("id", message_id), ("folder_id", str(folder))],
            binary=True,
        )
        if not isinstance(data, (bytes, bytearray)):
            raise ApiError(f"download {message_id}: no message source returned", body=data)
        return bytes(data)

    # -- changing --------------------------------------------------------------------------

    async def mark(
        self,
        folder: str,
        *,
        read: list[str] = (),
        unread: list[str] = (),
        flag: list[str] = (),
        unflag: list[str] = (),
    ) -> None:
        """Set/unset the unread and flagged marks (``POST api/v1/messages/marks``)."""
        marks = []
        fid = int(folder) if str(folder).isdigit() else folder
        if read or unread:
            marks.append({"name": "unread", "set": list(unread), "unset": list(read), "folder": fid})
        if flag or unflag:
            marks.append({"name": "flagged", "set": list(flag), "unset": list(unflag), "folder": fid})
        if not marks:
            return
        await self._call("api/v1/messages/marks", form=[("marks", _dumps(marks))])

    async def move(self, ids: list[str], folder: str) -> None:
        await self._call(
            "api/v1/messages/move", form=[("ids", _dumps(list(ids))), ("folder", str(folder))]
        )

    async def remove(self, ids: list[str]) -> None:
        """Delete messages (``api/v1/messages/remove``)."""
        await self._call("api/v1/messages/remove", form=[("ids", _dumps(list(ids)))])

    async def create_folder(self, name: str, parent: str = "-1") -> str:
        folder = {"id": -1, "name": name, "parent": parent, "only_web": False}
        body = (await self._call(
            "api/v1/folders/add",
            form=[("email", self.account.email), ("folders", _dumps([folder]))],
        )).get("body")
        if isinstance(body, list) and body:
            return str(body[0])
        raise ApiError("folders/add: no id returned", body=body)

    async def rename_folder(self, folder: str, name: str, parent: str | None = None) -> None:
        item: dict[str, Any] = {"id": int(folder), "name": name}
        if parent is not None:
            item["parent"] = parent
        await self._call(
            "api/v1/folders/edit",
            form=[("email", self.account.email), ("folders", _dumps([item]))],
        )

    async def delete_folder(self, folder: str) -> None:
        await self._call(
            "api/v1/folders/remove",
            form=[("email", self.account.email), ("ids", _dumps([int(folder)]))],
        )

    # -- sending ---------------------------------------------------------------------------

    async def upload_attachment(self, att: Attachment) -> str:
        files = {"file": (att.filename or "attachment", att.content, att.content_type)}
        body = (await self._call("api/v1/messages/attaches/add", form=[], files=files)).get("body")
        try:
            return str(body["attach"]["id"])
        except (TypeError, KeyError):
            raise ApiError("attaches/add: no attachment id returned", body=body) from None

    async def send(self, msg: OutgoingMessage) -> Any:
        return await self._compose("api/v1/messages/send", msg)

    async def save_draft(self, msg: OutgoingMessage) -> Any:
        """Save a draft (``api/v1/messages/draft``) — same body as send, nothing is dispatched."""
        return await self._compose("api/v1/messages/draft", msg)

    async def _compose(self, path: str, msg: OutgoingMessage) -> Any:
        attaches = []
        for att in msg.attachments:
            att_id = await self.upload_attachment(att)
            if att.content_id:
                attaches.append({"content_id": att.content_id, "type": "inline", "part_id": att_id})
            else:
                attaches.append({"id": att_id, "type": "attach"})
        body: dict[str, str] = {}
        if msg.html is not None:
            body["html"] = msg.html
        if msg.text is not None:
            body["text"] = msg.text
        correspondents = {"to": ", ".join(msg.to)}
        if msg.cc:
            correspondents["cc"] = ", ".join(msg.cc)
        if msg.bcc:
            correspondents["bcc"] = ", ".join(msg.bcc)
        form = [
            ("id", _new_message_id()),
            ("attaches", _dumps({"list": attaches})),
            ("body", _dumps(body)),
            ("correspondents", _dumps(correspondents)),
            ("from", msg.sender),
            ("priority", str(msg.priority)),
            ("receipt", "true" if msg.receipt else "false"),
            ("send_date", "0"),
            ("subject", msg.subject),
        ]
        source = {}
        if msg.reply_to_id:
            source["reply"] = msg.reply_to_id
        if msg.forward_of_id:
            source["forward"] = msg.forward_of_id
        if source:
            form.append(("source", _dumps(source)))
        return (await self._call(path, [("htmlencoded", "false")], form=form)).get("body")


_MSG_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _new_message_id() -> str:
    """A client-generated message id, as the app does (``RandomStringGenerator.generateString(32)``)."""
    import secrets

    return "".join(secrets.choice(_MSG_ID_ALPHABET) for _ in range(32))


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _is_not_found(body: Any) -> bool:
    text = json.dumps(body) if not isinstance(body, str) else body
    return "not_exist" in text


def make_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=20.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )
