import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mailru_bridge.config import BridgeConfig
from mailru_bridge.smtp_server import SmtpServer, SmtpSession


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


def session(reader=None):
    cfg = BridgeConfig()
    cfg.check_login = lambda user, password: user == "user@example.com" and password == "test-password"
    server = SmtpServer(SimpleNamespace(email="user@example.com"), None, cfg, AsyncMock())
    sess = SmtpSession(server, reader, Writer())
    sess.authed = True
    sess.greeted = True
    return sess


def test_rcpt_requires_mail_and_rset_clears_transaction():
    sess = session()

    async def run():
        await sess.smtp_rcpt("TO:<alice@example.com>")
        assert sess.rcpts == []
        assert b"503 MAIL required" in sess.writer.data
        await sess.smtp_mail("FROM:<>")
        assert sess.mail_started and sess.mail_from == ""
        await sess.smtp_rcpt("TO:<alice@example.com>")
        assert sess.rcpts == ["alice@example.com"]
        await sess.smtp_rset("")
        await sess.smtp_rcpt("TO:<alice@example.com>")
        assert not sess.mail_started and sess.rcpts == []
    asyncio.run(run())


@pytest.mark.parametrize("arg", ["TO:<sender@example.com>", "FROM:<missing-close", "FROM:sender@example.com",
                                    "FROM:<sender@example.com> SIZE=invalid", "FROM:<sender@example.com> SIZE=999999999"])
def test_invalid_mail_does_not_start_transaction(arg):
    sess = session()
    asyncio.run(sess.smtp_mail(arg))
    assert not sess.mail_started
    assert b"250 OK" not in sess.writer.data


def test_oversized_data_is_drained_and_rejected_without_losing_next_command(monkeypatch):
    monkeypatch.setattr("mailru_bridge.smtp_server.MAX_MESSAGE", 5)

    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"abcdef\r\nstill-data\r\n.\r\nNOOP\r\n")
        sess = session(reader)
        sess.mail_started, sess.rcpts = True, ["alice@example.com"]
        assert await sess.smtp_data("")
        sess.s.send_message.assert_not_awaited()
        assert b"552 message too large" in sess.writer.data
        assert not sess.mail_started and sess.rcpts == []
        assert await sess.read_line() == b"NOOP"
    asyncio.run(run())


def test_truncated_data_never_submits():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"Subject: s\r\n\r\nbody\r\n")
        reader.feed_eof()
        sess = session(reader)
        sess.mail_started, sess.rcpts = True, ["alice@example.com"]
        assert not await sess.smtp_data("")
        sess.s.send_message.assert_not_awaited()
        assert not sess.mail_started
    asyncio.run(run())


def test_protected_message_is_rejected_permanently_and_next_command_is_recoverable():
    from mailru_bridge.outgoing import build_outgoing
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Type: multipart/encrypted\r\n\r\nprotected\r\n.\r\nNOOP\r\n")
        sess = session(reader)
        deliver = AsyncMock()
        async def submit(raw, mail_from, rcpts):
            msg = build_outgoing(raw, mail_from, rcpts, "user@example.com")
            await deliver(msg)
        sess.s.send_message = submit
        sess.mail_started, sess.rcpts = True, ["recipient@example.com"]
        assert await sess.smtp_data("")
        assert b"554 5.6.0" in sess.writer.data and b"250 OK message accepted" not in sess.writer.data
        assert not sess.mail_started and not sess.rcpts
        deliver.assert_not_awaited()
        assert await sess.read_line() == b"NOOP"
    asyncio.run(run())


def test_login_accepts_initial_username_response_and_malformed_login_is_recoverable():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(base64.b64encode(b"test-password") + b"\r\n")
        sess = session(reader)
        sess.authed = False
        assert await sess.smtp_auth("LOGIN " + base64.b64encode(b"user@example.com").decode())
        assert sess.authed and b"235 authentication successful" in sess.writer.data
        sess.authed = False
        assert await sess.smtp_auth("LOGIN !!!invalid!!!")
        assert b"535 malformed LOGIN" in sess.writer.data
        assert not sess.authed
    asyncio.run(run())


def test_smtplib_over_local_socket():
    import smtplib
    sess = session()

    def client(port):
        with smtplib.SMTP("127.0.0.1", port, timeout=5) as smtp:
            smtp.login("user@example.com", "test-password")
            smtp.sendmail("user@example.com", ["alice@example.com", "hidden@example.com"],
                          b"Subject: test\r\n\r\nfirst\r\n.line\r\n")

    async def run():
        listener = await asyncio.start_server(sess.s._handle, "127.0.0.1", 0)
        async with listener:
            await asyncio.wait_for(asyncio.to_thread(client, listener.sockets[0].getsockname()[1]), 10)
        sess.s.send_message.assert_awaited_once_with(
            b"Subject: test\r\n\r\nfirst\r\n.line\r\n", "user@example.com",
            ["alice@example.com", "hidden@example.com"],
        )
    asyncio.run(run())


def test_submission_failure_logs_only_safe_validation_details(caplog):
    from mailru_bridge.api import ApiError

    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"Subject: private subject\r\n\r\nprivate body\r\n.\r\n")
        sess = session(reader)
        sess.s.send_message = AsyncMock(side_effect=ApiError("api/v1/messages/send: not found", 400, {
            "attaches.list[0].id": {"error": "not_exists", "value": "private-attachment-id"},
            "source.reply": {"error": "not_exist", "value": "private-message-id"},
            "from": {"error": "private-error-text", "value": "private@example.com"},
            "correspondents.to.private@example.com": {"error": "invalid", "value": "private-value"},
            "access_token": {"error": "required", "value": "private-token"},
        }))
        sess.mail_started, sess.rcpts = True, ["recipient@example.com"]
        assert await sess.smtp_data("")
        assert b"451 upstream submission failed" in sess.writer.data
        sess.s.send_message.assert_awaited_once()

    asyncio.run(run())
    assert "status=400" in caplog.text
    assert "attaches.list[0].id=not_exists" in caplog.text
    assert "source.reply=not_exist" in caplog.text
    assert "private" not in caplog.text and "access_token" not in caplog.text
