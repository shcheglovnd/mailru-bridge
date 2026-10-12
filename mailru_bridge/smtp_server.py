"""A minimal SMTP submission server that posts outgoing mail through the Mail.ru API.

A mail client submits a message here exactly as it would to any SMTP server; the bridge parses the
RFC 822 data, uploads any attachments, and calls ``api/v1/messages/send``. Mail.ru files the sent
copy in the Sent folder itself, so the client should not also append one.

Auth is the local bridge password (``AUTH LOGIN`` / ``AUTH PLAIN``), never the Mail.ru password.
Supports implicit TLS (when a certificate is configured) and ``STARTTLS`` on the plain port. Run it
behind a VPN.
"""

from __future__ import annotations

import asyncio
import base64
import logging
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
        self.tls = bool(server.config.tls)

    async def send(self, text: str) -> None:
        self.writer.write(text.encode() + b"\r\n")
        await self.writer.drain()

    async def read_line(self) -> bytes | None:
        try:
            line = await self.reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, ConnectionError):
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
                await self.send(f"502 command {cmd} not implemented")
                continue
            if not await handler(arg.strip()):
                return

    async def smtp_ehlo(self, arg: str) -> bool:
        feats = ["250-mailru-bridge", "250-AUTH LOGIN PLAIN", "250-8BITMIME", "250-SMTPUTF8"]
        if not self.tls and self.s.config.tls_cert:
            feats.append("250-STARTTLS")
        feats.append(f"250 SIZE {MAX_MESSAGE}")
        for f in feats:
            await self.send(f)
        return True

    async def smtp_helo(self, arg: str) -> bool:
        await self.send("250 mailru-bridge")
        return True

    async def smtp_starttls(self, arg: str) -> bool:
        if not self.s.config.tls_cert:
            await self.send("454 TLS not available")
            return True
        await self.send("220 Ready to start TLS")
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.s.config.tls_cert, self.s.config.tls_key)
        transport = self.writer.transport
        protocol = transport.get_protocol()
        loop = asyncio.get_event_loop()
        new_transport = await loop.start_tls(transport, protocol, ctx, server_side=True)
        protocol._stream_reader._transport = new_transport  # type: ignore[attr-defined]
        self.writer._transport = new_transport  # type: ignore[attr-defined]
        self.tls = True
        self.authed = False
        return True

    async def smtp_auth(self, arg: str) -> bool:
        mech, _, initial = arg.partition(" ")
        mech = mech.upper()
        if mech == "PLAIN":
            if not initial:
                await self.send("334 ")
                initial = (await self.read_line() or b"").decode()
            try:
                parts = base64.b64decode(initial).split(b"\x00")
                user, pw = parts[1].decode(), parts[2].decode()
            except Exception:
                await self.send("535 malformed PLAIN")
                return True
        elif mech == "LOGIN":
            await self.send("334 " + base64.b64encode(b"Username:").decode())
            user = base64.b64decode(await self.read_line() or b"").decode("utf-8", "replace")
            await self.send("334 " + base64.b64encode(b"Password:").decode())
            pw = base64.b64decode(await self.read_line() or b"").decode("utf-8", "replace")
        else:
            await self.send("504 unsupported auth mechanism")
            return True
        if self.s.config.check_login(user, pw):
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
        self.mail_from = _extract_addr(arg)
        self.rcpts = []
        await self.send("250 OK")
        return True

    async def smtp_rcpt(self, arg: str) -> bool:
        if not self.authed:
            await self.send("530 authentication required")
            return True
        addr = _extract_addr(arg)
        if addr:
            self.rcpts.append(addr)
            await self.send("250 OK")
        else:
            await self.send("501 bad recipient")
        return True

    async def smtp_rset(self, arg: str) -> bool:
        self.mail_from = ""
        self.rcpts = []
        await self.send("250 OK")
        return True

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
        if not self.rcpts:
            await self.send("503 no recipients")
            return True
        await self.send("354 End data with <CR><LF>.<CR><LF>")
        chunks = []
        size = 0
        while True:
            line = await self.reader.readuntil(b"\n")
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b".."):
                line = line[1:]  # dot-unstuffing
            chunks.append(line)
            size += len(line)
            if size > MAX_MESSAGE:
                await self.send("552 message too large")
                return True
        raw = b"".join(chunks)
        try:
            await self.s.send_message(raw, self.mail_from, list(self.rcpts))
            await self.send("250 OK message accepted")
        except ApiError as exc:
            log.warning("send failed: %s", exc)
            await self.send(f"554 send failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("send crashed")
            await self.send(f"451 internal error: {exc}")
        self.mail_from = ""
        self.rcpts = []
        return True


def _extract_addr(arg: str) -> str:
    """Pull the address out of ``FROM:<a@b>`` / ``TO:<a@b>`` (plus optional params)."""
    s = arg
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.strip()
    if s.startswith("<"):
        s = s[1 : s.find(">")] if ">" in s else s[1:]
    return s.split(" ", 1)[0].strip()
