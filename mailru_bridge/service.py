"""Run the bridge: the IMAP and SMTP front ends plus a gentle background sync, over one API client.

This is what ``mailru-bridge serve`` starts. It owns the shared :class:`~mailru_bridge.api.MailruApi`
(one rate-limited "device"), the local :class:`~mailru_bridge.cache.Cache`, the IMAP and SMTP
servers, and a background task that keeps the Inbox fresh so an idling client is told about new mail.
"""

from __future__ import annotations

import asyncio
import email
import logging
from email.policy import default as default_policy

from .api import Attachment, MailruApi, OutgoingMessage, RateLimiter
from .cache import Cache
from .config import BridgeConfig
from .imap_server import ImapServer
from .smtp_server import SmtpServer
from .store import Account
from .sync import Syncer

log = logging.getLogger(__name__)


def build_outgoing(raw: bytes, mail_from: str, rcpts: list[str], default_from: str) -> OutgoingMessage:
    """Turn a submitted RFC 822 message + SMTP envelope into an :class:`OutgoingMessage`.

    Recipients come from the SMTP envelope (so Bcc is honoured): an address is put in ``to``/``cc``
    according to the header it appears in, and anything left over (Bcc) goes in ``bcc``.
    """
    msg = email.message_from_bytes(raw, policy=default_policy)

    def addrs(header: str) -> list[str]:
        return [a.strip() for a in (msg.get(header, "") or "").replace("\n", " ").split(",") if a.strip()]

    header_to = {_bare(a): a for a in addrs("To")}
    header_cc = {_bare(a): a for a in addrs("Cc")}
    to, cc, bcc = [], [], []
    for r in rcpts:
        b = _bare(r)
        if b in header_to:
            to.append(header_to[b])
        elif b in header_cc:
            cc.append(header_cc[b])
        else:
            bcc.append(r)
    if not to and not cc and not bcc:  # fall back to the headers if envelope was empty
        to = addrs("To")
        cc = addrs("Cc")

    text = html = None
    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        if disp == "attachment" or (disp == "inline" and ctype.split("/")[0] not in ("text",)):
            payload = part.get_payload(decode=True) or b""
            cid = part.get("Content-ID")
            if cid:
                cid = cid.strip("<>")
            attachments.append(Attachment(
                filename=part.get_filename() or "attachment",
                content=payload,
                content_type=ctype,
                content_id=cid if disp == "inline" else None,
            ))
        elif ctype == "text/plain" and text is None:
            text = part.get_content()
        elif ctype == "text/html" and html is None:
            html = part.get_content()
    if text is None and html is None:
        text = ""

    return OutgoingMessage(
        sender=msg.get("From", mail_from or default_from),
        to=to, cc=cc, bcc=bcc,
        subject=msg.get("Subject", ""),
        text=text, html=html,
        attachments=attachments,
        priority=_priority(msg),
        receipt=bool(msg.get("Disposition-Notification-To")),
    )


def _bare(addr: str) -> str:
    from email.utils import parseaddr

    return parseaddr(addr)[1].lower()


def _priority(msg) -> int:
    p = (msg.get("X-Priority", "") or "").strip()
    if p.startswith("1") or p.startswith("2"):
        return 1
    if p.startswith("4") or p.startswith("5"):
        return 5
    return 3


class BridgeService:
    def __init__(self, account: Account, config: BridgeConfig, cache: Cache | None = None):
        self.account = account
        self.config = config
        self.api = MailruApi(account, limiter=RateLimiter(), on_auth_expired=self._on_auth_expired)
        self.cache = cache or Cache()
        self._last_alert = 0.0
        self.syncer = Syncer(self.api, self.cache)
        self.imap = ImapServer(account, self.api, self.cache, self.syncer, config)
        self.smtp = SmtpServer(account, self.api, config, self._send_message)

    async def _on_auth_expired(self, reason: str) -> None:
        """Alert the user (at most hourly) that the bridge needs a fresh `mailru-bridge login`."""
        import time

        now = time.monotonic()
        if now - self._last_alert < 3600:
            return
        self._last_alert = now
        log.error("authentication expired: %s", reason)
        cmd = self.config.alert_command
        if not cmd:
            return
        message = (
            "⚠️ mailru-bridge can't authenticate to Mail.ru and mail has stopped syncing.\n"
            f"Reason: {reason}\n"
            "Fix: run `mailru-bridge login` on the host."
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd.split(), message,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:  # noqa: BLE001 — a failed alert must not crash the bridge
            log.exception("alert command failed")

    async def _send_message(self, raw: bytes, mail_from: str, rcpts: list[str]) -> None:
        msg = build_outgoing(raw, mail_from, rcpts, self.account.email)
        log.info("sending message: %d recipient(s), %d attachment(s)", len(rcpts), len(msg.attachments))
        await self.api.send(msg)
        # Refresh Sent so the client sees the server-filed copy.
        try:
            await self.syncer.incremental_sync("500000", window=20)
        except Exception:  # noqa: BLE001
            log.debug("post-send Sent refresh failed", exc_info=True)

    async def _background_poll(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_seconds)
            try:
                folder = self.cache.folder("0")
                if folder and folder["last_sync"]:
                    await self.imap.incremental("0")
            except Exception:  # noqa: BLE001
                log.debug("background poll failed", exc_info=True)

    async def run(self) -> None:
        await self.imap.sync_folders()
        tasks = [
            asyncio.create_task(self.imap.serve()),
            asyncio.create_task(self.smtp.serve()),
            asyncio.create_task(self._background_poll()),
        ]
        log.info("bridge up: IMAP %s:%d, SMTP %s:%d (TLS=%s)",
                 self.config.bind_host, self.config.imap_port,
                 self.config.bind_host, self.config.smtp_port, self.config.tls)
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self.api.aclose()
            self.cache.close()
