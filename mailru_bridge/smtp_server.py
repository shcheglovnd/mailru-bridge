"""A minimal SMTP submission server that posts outgoing mail through the Mail.ru API.

A mail client submits a message here exactly as it would to any SMTP server; the bridge parses the
RFC 822 data, uploads any attachments, and calls ``api/v1/messages/send``. Mail.ru files the sent
copy in the Sent folder itself, so the client should not also append one.

Auth is the local bridge password (``AUTH LOGIN`` / ``AUTH PLAIN``), never the Mail.ru password.
Supports implicit TLS when a certificate/key pair is configured. Run it behind a VPN.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import ssl

from .api import ApiError, MailruApi
from .config import BridgeConfig

log = logging.getLogger(__name__)

MAX_MESSAGE = 40 * 1024 * 1024  # generous cap; Mail.ru itself limits attachment size


class SmtpServer:
    def __init__(self, account, api: MailruApi, config: BridgeConfig, send_message):
        self.account = account
        self.api = api
        self.config = config
        # send_message(raw_bytes, mail_from, rcpts) -> awaitable; injected so the service wires it
        # to the same parsing path the IMAP APPEND-to-draft uses.
        self.send_message = send_message

    async def serve(self) -> None:
        ssl_ctx = None
        if self.config.tls:
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(self.config.tls_cert, self.config.tls_key)
        server = await asyncio.start_server(
            self._handle, self.config.bind_host, self.config.smtp_port, ssl=ssl_ctx
        )
        addr = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("SMTP listening on %s (TLS=%s)", addr, bool(ssl_ctx))
        async with server:
            await server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sess = SmtpSession(self, reader, writer)
        try:
            await sess.run()
        except Exception:
            log.exception("smtp connection error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


class SmtpSession:
    def __init__(self, server: SmtpServer, reader, writer):
        self.s = server
        self.reader = reader
        self.writer = writer
        self.authed = False
        self.mail_from = ""
        self.rcpts: list[str] = []
        self.mail_started = False
        self.greeted = False
        self.tls = bool(server.config.tls)

    async def send(self, text: str) -> None:
        self.writer.write(text.encode() + b"\r\n")
        await self.writer.drain()

    async def read_line(self) -> bytes | None:
        try:
            line = await self.reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        except asyncio.LimitOverrunError:
            await self.send("500 line too long")
            return None
        return line.rstrip(b"\r\n")

    async def run(self) -> None:
        await self.send("220 mailru-bridge SMTP ready")
        while True:
            line = await self.read_line()
            if line is None:
                return
            try:
                cmd, _, arg = line.decode("utf-8", "replace").partition(" ")
            except Exception:
                await self.send("500 parse error")
                continue
            cmd = cmd.upper()
            handler = getattr(self, f"smtp_{cmd.lower()}", None)
            if handler is None:
                await self.send("502 command not implemented")
                continue
            if not await handler(arg.strip()):
                return

    async def smtp_ehlo(self, arg: str) -> bool:
        self.greeted = True
        self._reset_transaction()
        feats = ["250-mailru-bridge", "250-AUTH LOGIN PLAIN", "250-8BITMIME", "250-SMTPUTF8"]
        if not self.tls and self.s.config.tls_cert:
            feats.append("250-STARTTLS")
        feats.append(f"250 SIZE {MAX_MESSAGE}")
        for f in feats:
            await self.send(f)
        return True

    async def smtp_helo(self, arg: str) -> bool:
        self.greeted = True
        self._reset_transaction()
        await self.send("250 mailru-bridge")
        return True

    async def smtp_starttls(self, arg: str) -> bool:
        if self.tls:
            await self.send("503 TLS already active")
            return True
        if not self.s.config.tls_cert:
            await self.send("454 TLS not available")
            return True
        await self.send("220 Ready to start TLS")
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.s.config.tls_cert, self.s.config.tls_key)
        await self.writer.start_tls(ctx)
        self.tls = True
        self.authed = False
        self.greeted = False
        self._reset_transaction()
        return True

    async def smtp_auth(self, arg: str) -> bool:
        if not self.greeted or self.mail_started or self.authed:
            await self.send("503 AUTH not valid in current state")
            return True
        mech, _, initial = arg.partition(" ")
        mech = mech.upper()
        if mech == "PLAIN":
            if not initial:
                await self.send("334 ")
                initial = (await self.read_line() or b"").decode()
            try:
                parts = base64.b64decode(initial, validate=True).split(b"\x00")
                if len(parts) != 3 or (parts[0] and parts[0] != parts[1]):
                    raise ValueError("invalid authorization identity")
                user, pw = parts[1].decode(), parts[2].decode()
            except Exception:
                await self.send("535 malformed PLAIN")
                return True
        elif mech == "LOGIN":
            try:
                if not initial:
                    await self.send("334 " + base64.b64encode(b"Username:").decode())
                    initial = await self.read_line() or b""
                user = base64.b64decode(initial, validate=True).decode("utf-8")
                await self.send("334 " + base64.b64encode(b"Password:").decode())
                pw = base64.b64decode(await self.read_line() or b"", validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                await self.send("535 malformed LOGIN")
                return True
        else:
            await self.send("504 unsupported auth mechanism")
            return True
        if await asyncio.to_thread(self.s.config.check_login, user, pw):
            self.authed = True
            await self.send("235 authentication successful")
        else:
            await asyncio.sleep(1.0)
            await self.send("535 authentication failed")
        return True

    async def smtp_mail(self, arg: str) -> bool:
        if not self.authed:
            await self.send("530 authentication required")
            return True
        if self.mail_started:
            await self.send("503 transaction already started; use RSET")
            return True
        match = re.fullmatch(r"FROM:\s*<([^<>\s]*)>(?:\s+(.*))?", arg, re.IGNORECASE)
        if not match:
            await self.send("501 bad sender")
            return True
        for parameter in (match.group(2) or "").split():
            if parameter.upper().startswith("SIZE="):
                value = parameter[5:]
                if not value.isdigit():
                    await self.send("501 invalid SIZE")
                    return True
                if int(value) > MAX_MESSAGE:
                    await self.send("552 message too large")
                    return True
        self.mail_from = match.group(1)
        self.mail_started = True
        await self.send("250 OK")
        return True

    async def smtp_rcpt(self, arg: str) -> bool:
        if not self.authed:
            await self.send("530 authentication required")
            return True
        if not self.mail_started:
            await self.send("503 MAIL required before RCPT")
            return True
        match = re.fullmatch(r"TO:\s*<([^<>\s]+)>(?:\s+.*)?", arg, re.IGNORECASE)
        if match:
            addr = match.group(1)
            self.rcpts.append(addr)
            await self.send("250 OK")
        else:
            await self.send("501 bad recipient")
        return True

    async def smtp_rset(self, arg: str) -> bool:
        self._reset_transaction()
        await self.send("250 OK")
        return True

    def _reset_transaction(self) -> None:
        self.mail_from = ""
        self.rcpts = []
        self.mail_started = False

    async def smtp_noop(self, arg: str) -> bool:
        await self.send("250 OK")
        return True

    async def smtp_quit(self, arg: str) -> bool:
        await self.send("221 Bye")
        return False

    async def smtp_data(self, arg: str) -> bool:
        if not self.authed:
            await self.send("530 authentication required")
            return True
        if not self.mail_started or not self.rcpts:
            await self.send("503 no recipients")
            return True
        await self.send("354 End data with <CR><LF>.<CR><LF>")
        chunks = []
        size = 0
        try:
            while True:
                try:
                    line = await self.reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return False
                except asyncio.LimitOverrunError:
                    await self.send("552 message line too long")
                    return False
                if line in (b".\r\n", b".\n"):
                    break
                if line.startswith(b".."):
                    line = line[1:]  # dot-unstuffing
                size += len(line)
                if size <= MAX_MESSAGE:
                    chunks.append(line)
                else:
                    chunks.clear()
            if size > MAX_MESSAGE:
                await self.send("552 message too large")
                return True
            raw = b"".join(chunks)
            await self.s.send_message(raw, self.mail_from, list(self.rcpts))
            await self.send("250 OK message accepted")
        except ApiError as exc:
            log.warning("send failed: %s", exc)
            await self.send("451 upstream submission failed")
        except Exception as exc:  # noqa: BLE001
            log.exception("send crashed")
            await self.send("451 internal submission error")
        finally:
            self._reset_transaction()
        return True
