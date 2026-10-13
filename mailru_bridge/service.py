"""Run the bridge: the IMAP and SMTP front ends plus a gentle background sync, over one API client.

This is what ``mailru-bridge serve`` starts. It owns the shared :class:`~mailru_bridge.api.MailruApi`
(one rate-limited "device"), the local :class:`~mailru_bridge.cache.Cache`, the IMAP and SMTP
servers, and a background task that keeps all cached folders fresh for clients.
"""

from __future__ import annotations

import asyncio
import logging
import shlex

from .api import MailruApi, RateLimiter
from .cache import Cache
from .config import BridgeConfig
from .imap_server import ImapServer
from .outgoing import build_outgoing
from .smtp_server import SmtpServer
from .store import Account
from .sync import Syncer

log = logging.getLogger(__name__)


class BridgeService:
    def __init__(self, account: Account, config: BridgeConfig, cache: Cache | None = None):
        config.validate()
        self.account = account
        self.config = config
        self.api = MailruApi(account, limiter=RateLimiter(), on_auth_expired=self._on_auth_expired)
        self.cache = cache or Cache()
        self._last_alert: float | None = None
        self.syncer = Syncer(self.api, self.cache, flag_refresh_seconds=config.flag_refresh_seconds)
        self.imap = ImapServer(account, self.api, self.cache, self.syncer, config)
        self.smtp = SmtpServer(account, self.api, config, self._send_message)

    async def _on_auth_expired(self, reason: str) -> None:
        """Alert the user (at most hourly) that the bridge needs a fresh `mailru-bridge login`."""
        import time

        now = time.monotonic()
        if self._last_alert is not None and now - self._last_alert < 3600:
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
                *shlex.split(cmd), message,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                log.warning("alert command timed out")
        except Exception:  # noqa: BLE001 — a failed alert must not crash the bridge
            log.exception("alert command failed")

    async def _send_message(self, raw: bytes, mail_from: str, rcpts: list[str]) -> None:
        msg = build_outgoing(raw, mail_from, rcpts, self.account.email)
        msg.reply_to_id = self.cache.mailru_id_for_message_id(msg.in_reply_to)
        log.info("sending message: %d recipient(s), %d attachment(s)", len(rcpts), len(msg.attachments))
        await self.api.send(msg)
        # Refresh Sent so the client sees the server-filed copy.
        try:
            await self.imap.incremental("500000")
        except Exception:  # noqa: BLE001
            log.debug("post-send Sent refresh failed", exc_info=True)

    async def _background_poll(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_seconds)
            try:
                await self._poll_cached_folders()
            except Exception:  # noqa: BLE001
                log.debug("background poll failed", exc_info=True)

    async def _poll_cached_folders(self) -> None:
        await self.imap.sync_folders()
        for folder in self.cache.folders():
            if not folder["last_sync"]:
                continue
            try:
                await self.imap.incremental(folder["id"])
            except Exception:
                log.debug("background poll failed for folder %s", folder["id"], exc_info=True)

    async def run(self) -> None:
        tasks = []
        try:
            await self.imap.sync_folders()
            tasks = [
                asyncio.create_task(self.imap.serve()),
                asyncio.create_task(self.smtp.serve()),
                asyncio.create_task(self._background_poll()),
            ]
            log.info("bridge up: IMAP %s:%d, SMTP %s:%d (TLS=%s)",
                     self.config.bind_host, self.config.imap_port,
                     self.config.bind_host, self.config.smtp_port, self.config.tls)
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.api.aclose()
            self.cache.close()
