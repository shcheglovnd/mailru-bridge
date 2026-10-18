import asyncio
import logging
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mailru_bridge.cache import Cache
from mailru_bridge.config import BridgeConfig
from mailru_bridge.imap_server import ImapConnection, ImapServer, _split_astrings
from mailru_bridge.imap_utf7 import encode
from mailru_bridge.store import Account


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def get_extra_info(self, name):
        return None

    def close(self):
        pass


@pytest.fixture
def connection(tmp_path):
    cache = Cache(tmp_path / "cache.sqlite3")
    for fid, name in [("0", "INBOX"), ("500000", "Sent"), ("500001", "Drafts"),
                      ("500002", "Trash"), ("9", "Письма")]:
        cache.upsert_folder(fid, name, "-1", "user", False, 0, 0)
        cache.set_meta(f"deep:{fid}", "1")
    api = SimpleNamespace(**{name: AsyncMock() for name in
                            ("move", "remove", "mark", "send", "save_draft", "create_folder",
                             "delete_folder", "rename_folder")})
    api.save_draft.return_value = {"id": "1000"}
    syncer = SimpleNamespace(incremental_sync=AsyncMock(return_value=(0, [])),
                             initial_sync=AsyncMock(), sync_folders=AsyncMock())
    server = ImapServer(Account("user@example.com"), api, cache, syncer, BridgeConfig())
    conn = ImapConnection(server, None, Writer())
    yield conn
    cache.close()


def dispatch(conn, command, literals=()):
    tokens = [("t", b"A1 " + command)] + list(literals)
    return asyncio.run(conn.dispatch(tokens))


@pytest.mark.parametrize("command", [
    b'CREATE "new"', b'DELETE "Drafts"', b'RENAME "Drafts" "other"',
    b'APPEND Drafts', b'STATUS INBOX (MESSAGES)', b'SUBSCRIBE INBOX',
    b'NAMESPACE', b'UID EXPUNGE 1',
])
def test_mailbox_commands_require_authentication(connection, command):
    dispatch(connection, command)
    assert b"A1 NO LOGIN first" in connection.writer.data
    for value in vars(connection.s.api).values():
        value.assert_not_awaited()
    connection.s.syncer.incremental_sync.assert_not_awaited()


def select(conn, flags=()):
    conn.authed = True
    conn.selected = "0"
    for index, item in enumerate(flags, 1):
        conn.s.cache.add_message("0", str(index * 100), index, 1, set(item), {})
    conn._rebuild_snapshot()


@pytest.mark.parametrize("command", [b"STORE 1 +FLAGS (\\Deleted)", b"UID STORE 1 FLAGS ()",
                                        b"MOVE 1 Sent", b"UID MOVE 1 Sent", b"EXPUNGE",
                                        b"UID EXPUNGE 1"])
def test_readonly_rejects_mutation(connection, command):
    select(connection, [{"\\Deleted"}])
    connection.readonly = True
    dispatch(connection, command)
    assert b"A1 NO mailbox is read-only" in connection.writer.data
    assert connection.s.cache.message_by_uid("0", 1) is not None
    connection.s.api.move.assert_not_awaited()
    connection.s.api.mark.assert_not_awaited()


def test_uid_star_uses_highest_uid_after_expunges(connection):
    select(connection, [set(), set(), set()])
    connection.s.cache.forget("0", [1, 2])
    connection._rebuild_snapshot()
    dispatch(connection, b"UID FETCH * FLAGS")
    assert b"1 FETCH (FLAGS () UID 3)" in connection.writer.data


def test_uid_expunge_only_removes_requested_deleted_uids(connection):
    select(connection, [{"\\Deleted"}, {"\\Deleted"}, {"\\Seen"}])
    dispatch(connection, b"UID EXPUNGE 2:3")
    connection.s.api.move.assert_awaited_once_with(["200"], "500002")
    assert [m.uid for m in connection.s.cache.messages("0")] == [1, 3]
    assert b"2 EXPUNGE" in connection.writer.data
    assert b"1 EXPUNGE" not in connection.writer.data


def test_uid_move_parses_subcommand_arguments(connection):
    select(connection, [set(), set()])
    dispatch(connection, b"UID MOVE 2 Sent")
    connection.s.api.move.assert_awaited_once_with(["200"], "500000")
    assert [m.uid for m in connection.s.cache.messages("0")] == [1]


def test_draft_append_never_sends_and_preserves_attachments(connection):
    connection.authed = True
    msg = EmailMessage()
    msg["To"] = '"Example, Alice" <alice@example.com>'
    msg.set_content("unsent body")
    msg.add_attachment(b"FILE", maintype="application", subtype="octet-stream", filename="f.bin")
    dispatch(connection, b"APPEND Drafts ", [("l", msg.as_bytes()), ("t", b"")])
    connection.s.api.send.assert_not_awaited()
    outgoing = connection.s.api.save_draft.await_args.args[0]
    assert outgoing.text.strip() == "unsent body"
    assert outgoing.to == ['"Example, Alice" <alice@example.com>']
    assert outgoing.attachments[0].content == b"FILE"


def test_thunderbird_draft_append_returns_uid_and_preserves_flags_and_internaldate(connection):
    connection.authed = True
    raw = b"Message-ID: <draft@example.com>\r\nSubject: unsent\r\n\r\nbody"
    dispatch(connection, b'APPEND Drafts (\\Seen \\Draft $label1) "12-Oct-2026 17:00:00 -0500" ',
             [("l", raw), ("t", b"")])
    validity = connection.s.cache.folder("500001")["uidvalidity"]
    assert f"A1 OK [APPENDUID {validity} 1]".encode() in connection.writer.data
    draft = connection.s.cache.message_by_uid("500001", 1)
    assert draft.flags == {"\\Seen", "\\Draft", "$label1"}
    assert draft.date == 1791842400  # supplied date, converted to UTC
    connection.s.api.mark.assert_awaited_once_with("500001", read=["1000"])
    connection.selected = "500001"
    connection._rebuild_snapshot()
    dispatch(connection, b"UID SEARCH UNDELETED HEADER Message-ID <draft@example.com>")
    assert b"* SEARCH 1\r\n" in connection.writer.data


def test_draft_append_uses_last_literal_when_mailbox_itself_is_literal(connection):
    connection.authed = True
    raw = b"Subject: actual draft\r\n\r\nbody"
    dispatch(connection, b"APPEND ", [("l", b"Drafts"), ("t", b" (\\Draft) "),
                                      ("l", raw), ("t", b"")])
    assert connection.s.api.save_draft.await_args.args[0].subject == "actual draft"
    assert connection.s.cache.get_raw("500001", 1) == raw


def test_draft_followup_read_mark_failure_does_not_report_failed_save(connection):
    from mailru_bridge.api import ApiError
    connection.authed = True
    connection.s.api.mark.side_effect = ApiError("temporary failure")
    dispatch(connection, b"APPEND Drafts (\\Seen \\Draft) ",
             [("l", b"Subject: saved\r\n\r\nbody"), ("t", b"")])
    assert b"A1 OK [APPENDUID " in connection.writer.data
    assert "\\Seen" not in connection.s.cache.message_by_uid("500001", 1).flags
    connection.s.api.save_draft.assert_awaited_once()


def test_successful_draft_save_without_remote_id_is_not_retried_or_guessed(connection):
    connection.authed = True
    connection.s.api.save_draft.return_value = {}
    dispatch(connection, b"APPEND Drafts ", [("l", b"Subject: saved\r\n\r\nbody"), ("t", b"")])
    assert b"A1 OK APPEND (draft) completed" in connection.writer.data
    assert b"APPENDUID" not in connection.writer.data
    connection.s.api.save_draft.assert_awaited_once()
    connection.s.syncer.incremental_sync.assert_awaited_once()
    assert connection.s.cache.messages("500001") == []


def test_protected_draft_is_rejected_without_saving_or_sending(connection):
    connection.authed = True
    dispatch(connection, b"APPEND Drafts ",
             [("l", b"Content-Type: multipart/encrypted\r\n\r\nprotected"), ("t", b"")])
    assert b"A1 NO PGP/MIME and S/MIME" in connection.writer.data
    connection.s.api.save_draft.assert_not_awaited()
    connection.s.api.send.assert_not_awaited()


@pytest.mark.parametrize("options", [b"(\\Recent) ", b"(\\Unknown) ", b'"not-a-date" ', b"(tag) (other) "])
def test_invalid_append_options_rejected_before_saving(connection, options):
    connection.authed = True
    with pytest.raises(ValueError):
        dispatch(connection, b"APPEND Drafts " + options,
                 [("l", b"Subject: unsent\r\n\r\nbody"), ("t", b"")])
    connection.s.api.save_draft.assert_not_awaited()


def test_move_returns_exact_uid_mapping_before_expunge(connection):
    select(connection, [set(), set(), set()])
    connection.s.cache.add_message("9", "other", 0, 1, set(), {})
    connection.s.cache.forget("0", [2])  # stale selected snapshot; skip vanished UID
    dispatch(connection, b'UID MOVE 1:3 "' + encode("Письма") + b'"')
    validity = connection.s.cache.folder("9")["uidvalidity"]
    code = f"* OK [COPYUID {validity} 1,3 2,3] moved\r\n".encode()
    assert code in connection.writer.data
    assert connection.writer.data.index(code) < connection.writer.data.index(b"EXPUNGE")
    assert [m.mailru_id for m in connection.s.cache.messages("9")] == ["other", "100", "300"]


def test_two_clients_moving_same_message_only_first_calls_api(connection):
    async def run():
        select(connection, [set()])
        second = ImapConnection(connection.s, None, Writer())
        second.authed, second.selected, second.snapshot = True, "0", [1]
        started, finish = asyncio.Event(), asyncio.Event()
        async def move(ids, destination):
            started.set()
            await finish.wait()
        connection.s.api.move.side_effect = move
        first = asyncio.create_task(connection.dispatch([("t", b"A1 UID MOVE 1 Sent")]))
        await started.wait()
        other = asyncio.create_task(second.dispatch([("t", b"A2 UID MOVE 1 Drafts")]))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(first, other)
        connection.s.api.move.assert_awaited_once_with(["100"], "500000")
        assert connection.s.cache.uid_for("500000", "100") == 1
        assert connection.s.cache.messages("500001") == []
    asyncio.run(run())


def test_thunderbird_tags_and_reply_markers_survive_remote_sync(connection):
    select(connection, [set()])
    dispatch(connection, b"UID STORE 1 +FLAGS.SILENT (\\Answered $Forwarded $LABEL1 customtag Junk)")
    connection.s.cache.update_remote_messages_batch("0", [("100", 1, 1, {"\\Seen"}, {})])
    dispatch(connection, b"UID FETCH 1 FLAGS")
    flags = connection.s.cache.message_by_uid("0", 1).flags
    assert flags == {"\\Seen", "\\Answered", "$Forwarded", "$label1", "customtag", "junk"}
    dispatch(connection, b"UID STORE 1 -FLAGS.SILENT ($LABEL1 CUSTOMTAG)")
    dispatch(connection, b"UID SEARCH KEYWORD junk")
    assert b"* SEARCH 1\r\n" in connection.writer.data
    connection.s.api.mark.assert_not_awaited()


def test_thunderbird_sync_chunks_move_and_repeated_draft_saves_over_socket(connection):
    from mailru_bridge.sync import Syncer
    import re

    async def run():
        select(connection, [set()])
        raw = (b"From: sender@example.com\r\nTo: user@example.com\r\n"
               b"Message-ID: <original@example.com>\r\nSubject: original\r\n"
               b"References: <earlier@example.com>\r\nContent-Type: text/plain\r\n\r\n"
               + b"chunked body\r\n" * 700)
        connection.s.cache.set_raw("0", 1, raw)
        connection.s.syncer.ensure_raw = Syncer(connection.s.api, connection.s.cache).ensure_raw
        connection.s.config.check_login = lambda user, password: True
        connection.s.api.save_draft.side_effect = [{"id": "1000"}, {"id": "2000"}]
        listener = await asyncio.start_server(connection.s._handle, "127.0.0.1", 0)
        async with listener:
            reader, writer = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
            assert b"UIDPLUS" in await reader.readline()
            async def command(tag, data):
                writer.write(tag + b" " + data + b"\r\n")
                await writer.drain()
                lines, literals = [], []
                while True:
                    line = await asyncio.wait_for(reader.readline(), 5)
                    assert line
                    lines.append(line)
                    match = re.search(rb"\{(\d+)\}\r\n$", line)
                    if match:
                        literals.append(await reader.readexactly(int(match[1])))
                    if line.startswith(tag + b" "):
                        assert line.startswith(tag + b" OK "), line
                        return b"".join(lines), literals
            try:
                await command(b"a", b'LOGIN "user@example.com" "test-password"')
                await command(b"b", b"NAMESPACE")
                listed, _ = await command(b"c", b'LSUB "" "*"')
                assert b'\\Drafts) "/" "Drafts"' in listed
                selected, _ = await command(b"d", b"SELECT INBOX")
                assert b"\\*" in selected
                headers = (b"From To Cc Bcc Subject Date Message-ID Priority X-Priority References "
                           b"Newsgroups In-Reply-To Content-Type Reply-To Received")
                response, parts = await command(b"e", b"UID FETCH 1:* (UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS ("
                                                      + headers + b")])")
                assert b"References: <earlier@example.com>" in parts[0]
                assert f"RFC822.SIZE {len(raw)}".encode() in response
                await command(b"f", b"UID STORE 1 +FLAGS.SILENT ($label1 \\Answered $Forwarded)")
                for tag, start, length in [(b"g", 0, 4096), (b"h", 4096, 4096), (b"i", len(raw), 4096)]:
                    response, parts = await command(tag, f"UID FETCH 1 (UID RFC822.SIZE BODY.PEEK[]<{start}.{length}>)".encode())
                    assert parts == [raw[start:start + length]]
                    assert f"BODY[]<{start}>".encode() in response
                moved, _ = await command(b"j", b"UID MOVE 1 Sent")
                assert b"COPYUID" in moved and b"1 EXPUNGE" in moved
                await command(b"k", b"SELECT Drafts")
                for tag, uid in [(b"l", 1), (b"m", 2)]:
                    draft = (b"To: recipient@example.com\r\nIn-Reply-To: <original@example.com>\r\n"
                             b"Message-ID: <draft@example.com>\r\nX-Mozilla-Draft-Info: internal/draft; vcard=0\r\n"
                             b"Subject: unsent revision " + str(uid).encode() + b"\r\n\r\nbody")
                    response, _ = await command(tag, b'APPEND "Drafts" (\\Seen \\Draft) "12-Oct-2026 17:00:00 -0500" {'
                                                  + str(len(draft)).encode() + b"+}\r\n" + draft)
                    assert f" {uid}] APPEND (draft) completed".encode() in response
                    assert b"APPENDUID" in response and f"* {uid} EXISTS".encode() in response
                assert connection.s.api.save_draft.await_args.args[0].reply_to_id == "100"
                await command(b"n", b"UID STORE 1 +FLAGS.SILENT (\\Deleted)")
                await command(b"o", b"UID EXPUNGE 1")
                response, _ = await command(b"p", b"UID SEARCH UNDELETED")
                assert b"* SEARCH 2\r\n" in response
                assert connection.s.cache.live_mailru_ids("500001") == {"2000"}
                connection.s.api.send.assert_not_awaited()
                await command(b"q", b"LOGOUT")
            finally:
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_sent_append_is_not_acknowledged_without_storing(connection):
    connection.authed = True
    dispatch(connection, b"APPEND Sent ", [("l", b"Subject: s\r\n\r\nbody"), ("t", b"")])
    assert b"A1 NO Sent is server-managed" in connection.writer.data
    connection.s.api.send.assert_not_awaited()


def test_poll_reconciles_changes_already_applied_by_another_client(connection):
    select(connection, [set(), set()])
    connection.s.cache.forget("0", [1])
    connection.s.cache.update_flags("0", 2, {"\\Seen"})
    connection.s.cache.add_message("0", "300", 3, 1, set(), {})
    asyncio.run(connection.poll_selected())
    assert connection.snapshot == [2, 3]
    assert b"1 EXPUNGE" in connection.writer.data
    assert b"2 EXISTS" in connection.writer.data
    assert b"1 FETCH (UID 2 FLAGS (\\Seen))" in connection.writer.data


def test_manual_refresh_publishes_seen_change_from_another_client(connection):
    select(connection, [set()])
    desktop = ImapConnection(connection.s, None, Writer())
    desktop.authed, desktop.selected = True, "0"
    desktop._rebuild_snapshot()
    dispatch(desktop, b"UID STORE 1 +FLAGS.SILENT (\\Seen)")
    connection.s.api.mark.assert_awaited_once_with("0", read=["100"])
    dispatch(connection, b"UID SEARCH UNDELETED")
    flag_update = b"* 1 FETCH (UID 1 FLAGS (\\Seen))\r\n"
    assert flag_update in connection.writer.data
    assert connection.writer.data.index(flag_update) < connection.writer.data.index(b"* SEARCH 1\r\n")


def test_cached_flag_refresh_defers_expunge_and_uses_original_sequence_numbers(connection):
    select(connection, [set(), set()])
    connection.s.cache.forget("0", [1])
    connection.s.cache.update_flags("0", 2, {"\\Seen"})
    dispatch(connection, b"UID SEARCH ALL")
    assert b"* 2 FETCH (UID 2 FLAGS (\\Seen))\r\n" in connection.writer.data
    assert b"EXPUNGE" not in connection.writer.data
    connection.writer.data.clear()
    dispatch(connection, b"NOOP")
    assert b"* 1 EXPUNGE\r\n" in connection.writer.data
    assert connection.snapshot == [2]


def test_thunderbird_read_reaches_fairemail_idle_and_manual_refresh_over_sockets(connection):
    from mailru_bridge.sync import Syncer
    import re
    async def run():
        select(connection, [set()])
        connection.s.cache.set_raw("0", 1, b"Subject: original\r\n\r\nbody")
        connection.s.syncer.ensure_raw = Syncer(connection.s.api, connection.s.cache).ensure_raw
        connection.s.config.check_login = lambda user, password: True
        listener = await asyncio.start_server(connection.s._handle, "127.0.0.1", 0)
        async with listener:
            phone_r, phone_w = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
            desktop_r, desktop_w = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
            async def command(reader, writer, tag, command):
                writer.write(tag + b" " + command + b"\r\n")
                await writer.drain()
                lines = []
                while True:
                    line = await asyncio.wait_for(reader.readline(), 1)
                    assert line
                    lines.append(line)
                    literal = re.search(rb"\{(\d+)\}\r\n$", line)
                    if literal:
                        await reader.readexactly(int(literal[1]))
                    if line.startswith(tag + b" "):
                        assert line.startswith(tag + b" OK "), line
                        return b"".join(lines)
            try:
                for reader, writer in [(phone_r, phone_w), (desktop_r, desktop_w)]:
                    assert b"mailru-bridge ready" in await reader.readline()
                    await command(reader, writer, b"login", b"LOGIN user@example.com test-password")
                    await command(reader, writer, b"select", b"SELECT INBOX")
                polls = connection.s.syncer.incremental_sync.await_count
                phone_w.write(b"idle IDLE\r\n")
                await phone_w.drain()
                assert await phone_r.readline() == b"+ idling\r\n"
                # Thunderbird opens a body, implicitly marking the message read upstream.
                await command(desktop_r, desktop_w, b"read", b"UID FETCH 1 BODY[]")
                assert await asyncio.wait_for(phone_r.readline(), 1) == b"* 1 FETCH (UID 1 FLAGS (\\Seen))\r\n"
                connection.s.api.mark.assert_awaited_once_with("0", read=["100"])
                assert connection.s.syncer.incremental_sync.await_count == polls
                # Unread changes propagate too, without waiting for the 180-second timer.
                await command(desktop_r, desktop_w, b"unread", b"UID STORE 1 -FLAGS.SILENT (\\Seen)")
                assert await asyncio.wait_for(phone_r.readline(), 1) == b"* 1 FETCH (UID 1 FLAGS ())\r\n"
                phone_w.write(b"DONE\r\n")
                await phone_w.drain()
                assert await phone_r.readline() == b"idle OK IDLE terminated\r\n"
                # During a manual refresh, SEARCH also publishes changes already in cache.
                await command(desktop_r, desktop_w, b"seen", b"UID STORE 1 +FLAGS.SILENT (\\Seen)")
                result = await command(phone_r, phone_w, b"refresh", b"UID SEARCH UNDELETED")
                assert b"* 1 FETCH (UID 1 FLAGS (\\Seen))\r\n" in result
                assert b"* SEARCH 1\r\n" in result
                await command(phone_r, phone_w, b"bye", b"LOGOUT")
                await command(desktop_r, desktop_w, b"bye", b"LOGOUT")
            finally:
                phone_w.close()
                desktop_w.close()
                await asyncio.gather(phone_w.wait_closed(), desktop_w.wait_closed())
        assert not connection.s._connections
    asyncio.run(run())


def test_remote_sync_wakes_selected_connection_after_web_flag_change(connection):
    async def run():
        select(connection, [set()])
        connection.s._connections.add(connection)
        async def remote_sync(fid, window):
            connection.s.cache.update_remote_flags(fid, 1, {"\\Seen"})
            return 0, []
        connection.s.syncer.incremental_sync.side_effect = remote_sync
        await connection.s.incremental("0")
        assert connection._changed.is_set()
        await connection._reconcile_cached(allow_expunge=True)
        assert b"* 1 FETCH (UID 1 FLAGS (\\Seen))\r\n" in connection.writer.data
    asyncio.run(run())


def test_modified_utf7_select_and_namespace(connection):
    connection.authed = True
    dispatch(connection, b'SELECT "' + encode("Письма") + b'"')
    assert connection.selected == "9"
    dispatch(connection, b"NAMESPACE")
    assert b'NAMESPACE (("" "/")) NIL NIL' in connection.writer.data
    assert "UIDPLUS" in connection.s.capabilities()


@pytest.mark.parametrize("criteria, expected", [
    (b"UNFLAGGED", b"SEARCH 1 3"), (b"UNSEEN FLAGGED", b"SEARCH 2"),
    (b"NOT SEEN", b"SEARCH 2 3"), (b"OR SEEN FLAGGED", b"SEARCH 1 2"),
    (b"(UNSEEN UNFLAGGED)", b"SEARCH 3"), (b"UID 2:*", b"SEARCH 2 3"),
])
def test_search_predicates(connection, criteria, expected):
    select(connection, [{"\\Seen"}, {"\\Flagged"}, set()])
    dispatch(connection, b"UID SEARCH " + criteria)
    assert expected + b"\r\n" in connection.writer.data


def test_unsupported_search_returns_bad_even_for_empty_folder(connection):
    select(connection)
    dispatch(connection, b'SEARCH UNKNOWN "hello"')
    assert b"A1 BAD unsupported SEARCH predicate UNKNOWN" in connection.writer.data


@pytest.mark.parametrize("data", [b")", b'"unterminated', b"(unterminated", b"a )"])
def test_malformed_astrings_fail_without_hanging(data):
    with pytest.raises(ValueError):
        _split_astrings([("t", data)])


def test_slow_login_logs_no_credentials(connection, monkeypatch, caplog):
    import time
    def check_login(user, password):
        time.sleep(1.01)
        return True
    connection.s.config.check_login = check_login
    with caplog.at_level(logging.INFO):
        dispatch(connection, b'LOGIN "user@example.com" "private-client-password"')
    assert "LOGIN" in caplog.text
    assert "private-client-password" not in caplog.text
    assert "user@example.com" not in caplog.text


def test_oversized_preauth_literal_is_rejected_before_continuation(connection):
    async def run():
        connection.reader = asyncio.StreamReader()
        connection.reader.feed_data(b"A1 LOGIN user {999999999}\r\n")
        return await connection.read_command()
    assert asyncio.run(run()) is None
    assert b"BYE command literal too large" in connection.writer.data
    assert b"+ Ready" not in connection.writer.data


def test_idle_cancellation_stops_the_reader_task(connection):
    select(connection)

    async def run():
        connection.reader = asyncio.StreamReader()
        task = asyncio.create_task(connection.cmd_idle(b"A1", b"", []))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not connection._idle
        connection.reader.feed_data(b"next\r\n")
        assert await connection.read_line() == b"next"

    asyncio.run(run())


def test_cached_mailbox_commands_do_not_wait_behind_busy_sync(connection):
    async def run():
        select(connection, [set()])
        connection.s.cache.set_last_sync("0", 100)
        async with connection.s._lock:
            for command in (b'LIST "" "*"', b'LSUB "" "*"', b'STATUS INBOX (MESSAGES UNSEEN)',
                            b'SELECT INBOX', b'NOOP', b'CHECK', b'UID SEARCH ALL'):
                await asyncio.wait_for(connection.dispatch([("t", b"A1 " + command)]), .5)
        assert b"* SEARCH 1\r\n" in connection.writer.data
        assert connection.writer.data.count(b"A1 OK ") == 7
        connection.s.syncer.sync_folders.assert_not_awaited()
        connection.s.syncer.incremental_sync.assert_not_awaited()
    asyncio.run(run())


def test_folder_mutations_still_force_authoritative_tree_refresh(connection):
    connection.authed = True
    dispatch(connection, b'CREATE "new"')
    connection.s.api.create_folder.assert_awaited_once_with("new", "-1")
    connection.s.syncer.sync_folders.assert_awaited_once()


def test_background_sync_still_waits_for_mutation_lock(connection):
    async def run():
        async with connection.s._lock:
            task = asyncio.create_task(connection.s.incremental("0"))
            await asyncio.sleep(0)
            assert not task.done()
            connection.s.syncer.incremental_sync.assert_not_awaited()
        await asyncio.wait_for(task, .5)
        connection.s.syncer.incremental_sync.assert_awaited_once()
    asyncio.run(run())


def test_fetch_streams_ready_message_before_slow_ones_and_bounds_active_downloads(connection):
    async def run():
        select(connection, [set() for _ in range(30)])
        release, first_response = asyncio.Event(), asyncio.Event()
        started = set()
        async def fetch_one(seq, uid, items, by_uid):
            started.add(uid)
            if uid != 2:
                await release.wait()
            return f"{seq} FETCH (UID {uid} BODY[HEADER] {{0}}\r\n)".encode()
        connection._fetch_one = fetch_one
        old_drain = connection.writer.drain
        async def drain():
            await old_drain()
            if b"* 2 FETCH (UID 2 " in connection.writer.data:
                first_response.set()
        connection.writer.drain = drain
        task = asyncio.create_task(connection.dispatch([("t", b"A1 UID FETCH 1:* BODY.PEEK[HEADER]")]))
        try:
            await asyncio.wait_for(first_response.wait(), .5)
            await asyncio.sleep(0)
            assert not task.done()
            assert b"* 1 FETCH" not in connection.writer.data
            assert len(started) <= 11  # one returned plus at most ten active
            release.set()
            await asyncio.wait_for(task, .5)
            assert len(started) == 30
            assert connection.writer.data.count(b" FETCH (") == 30
            assert b"A1 OK UID FETCH completed" in connection.writer.data
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_fetch_disconnect_cancels_other_downloads_and_stops_scheduling(connection):
    async def run():
        select(connection, [set() for _ in range(30)])
        pending, cancelled = set(), set()
        async def fetch_one(seq, uid, items, by_uid):
            pending.add(uid)
            if uid == 1:
                return b"1 FETCH (UID 1 BODY[HEADER] {0}\r\n)"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(uid)
        async def broken_drain():
            raise ConnectionResetError("client closed")
        connection._fetch_one = fetch_one
        connection.writer.drain = broken_drain
        with pytest.raises(ConnectionResetError):
            await connection.cmd_fetch(b"A1", b"1:* BODY.PEEK[HEADER]", [], by_uid=True)
        assert len(pending) <= 10
        assert cancelled == pending - {1}
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    asyncio.run(run())


def test_client_reset_is_not_retried_as_an_internal_error(connection, caplog):
    async def run():
        connection.reader = asyncio.StreamReader()
        connection.reader.feed_data(b"A1 NOOP\r\n")
        connection.reader.feed_eof()
        calls = 0
        async def drain():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ConnectionResetError("client closed")
        connection.writer.drain = drain
        with caplog.at_level(logging.ERROR):
            await connection.serve()
        assert calls == 2  # greeting and response, no attempt at a second error response
        assert "command failed" not in caplog.text
    asyncio.run(run())


def test_idle_survives_temporary_api_failure_until_done(connection):
    from mailru_bridge.api import ApiError
    async def run():
        select(connection)
        connection.reader = asyncio.StreamReader()
        connection.s.config.poll_seconds = .01
        retried = asyncio.Event()
        polls = 0
        async def poll():
            nonlocal polls
            polls += 1
            if polls == 1:
                raise ApiError("temporary upstream failure")
            retried.set()
            connection.reader.feed_data(b"DONE\r\n")
        connection.poll_selected = poll
        task = asyncio.create_task(connection.cmd_idle(b"A1", b"", []))
        try:
            await asyncio.wait_for(retried.wait(), .5)
            await asyncio.wait_for(task, .5)
            assert b"A1 OK IDLE terminated" in connection.writer.data
            assert b"A1 NO" not in connection.writer.data
            assert not connection._idle
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_imaplib_over_local_socket(connection):
    import imaplib
    select(connection, [set(), {"\\Seen"}])
    connection.s.config.check_login = lambda user, password: user == "user@example.com" and password == "test-password"

    def client(port):
        with imaplib.IMAP4("127.0.0.1", port) as imap:
            assert imap.login("user@example.com", "test-password")[0] == "OK"
            assert imap.select("INBOX")[1] == [b"2"]
            assert imap.uid("FETCH", "*", "(UID FLAGS)")[0] == "OK"
            assert imap.uid("STORE", "1", "+FLAGS", "(\\Flagged)")[0] == "OK"
            assert imap.uid("SEARCH", None, "UNSEEN", "FLAGGED")[1] == [b"1"]
            assert imap.namespace()[0] == "OK"

    async def run():
        listener = await asyncio.start_server(connection.s._handle, "127.0.0.1", 0)
        async with listener:
            await asyncio.wait_for(asyncio.to_thread(client, listener.sockets[0].getsockname()[1]), 10)
    asyncio.run(run())


def test_idle_poll_reports_read_updates_to_messages_older_than_newest_window(connection):
    select(connection, [set() for _ in range(205)])
    # Folder-list reconciliation repaired the old messages before this client's IDLE tick.
    connection.s.cache.mark_all_seen("0")
    asyncio.run(connection.poll_selected())
    assert b"1 FETCH (UID 1 FLAGS (\\Seen))" in connection.writer.data
    assert b"205 FETCH (UID 205 FLAGS (\\Seen))" in connection.writer.data
    assert len(connection.snapshot) == 205


def test_shallow_status_cache_keeps_refreshing_without_select(connection):
    connection.s.cache.set_meta("deep:9", "0")
    connection.s.cache.set_last_sync("9", 100)
    asyncio.run(connection.s.sync_folder_quietly("9"))
    connection.s.syncer.incremental_sync.assert_awaited_once_with("9", window=connection.s.config.sync_window)


def test_fairemail_date_search_includes_recent_arrival(connection):
    from datetime import datetime, timezone
    select(connection)
    for mid, day in [("100", 1), ("200", 6), ("300", 13)]:
        when = int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp())
        connection.s.cache.add_message("0", mid, when, 1, set(), {})
    connection._rebuild_snapshot()
    dispatch(connection, b"SEARCH SINCE 5-Oct-2026")
    assert b"* SEARCH 2 3\r\n" in connection.writer.data
    assert b"A1 OK SEARCH completed" in connection.writer.data


def test_search_notices_mail_arriving_after_select_without_idle_or_noop(connection):
    select(connection, [set()])
    connection.s.cache.add_message("0", "200", 2, 1, set(), {})
    dispatch(connection, b"UID SEARCH ALL")
    assert connection.snapshot == [1, 2]
    assert b"* 2 EXISTS\r\n" in connection.writer.data
    assert b"* SEARCH 1 2\r\n" in connection.writer.data


def test_manual_search_polls_remote_before_returning_uids(connection):
    select(connection, [set()])
    async def incoming(fid, window):
        connection.s.cache.add_message(fid, "200", 2, 1, set(), {})
        return 1, []
    connection.s.syncer.incremental_sync.side_effect = incoming
    dispatch(connection, b"UID SEARCH UID 2:*")
    connection.s.syncer.incremental_sync.assert_awaited_once()
    assert b"* SEARCH 2\r\n" in connection.writer.data


def test_fetch_can_read_an_arrival_already_added_by_background_poll(connection):
    select(connection, [set()])
    connection.s.cache.add_message("0", "200", 2, 1, set(), {})
    dispatch(connection, b"UID FETCH 2 (UID FLAGS)")
    assert b"* 2 EXISTS\r\n" in connection.writer.data
    assert b"* 2 FETCH (UID 2 FLAGS ())\r\n" in connection.writer.data


def test_search_defers_expunge_and_preserves_sequence_numbers(connection):
    select(connection, [set(), set(), set()])
    connection.s.cache.forget("0", [1])
    connection.s.cache.add_message("0", "400", 4, 1, set(), {})
    dispatch(connection, b"SEARCH ALL")
    assert b"EXPUNGE" not in connection.writer.data
    assert b"* SEARCH 2 3 4\r\n" in connection.writer.data
    assert connection.snapshot == [1, 2, 3, 4]
    connection.writer.data.clear()
    dispatch(connection, b"NOOP")
    assert b"* 1 EXPUNGE\r\n" in connection.writer.data
    assert connection.snapshot == [2, 3, 4]


def test_fairemail_refresh_sequence_over_local_socket(connection):
    import imaplib
    from datetime import datetime, timezone
    select(connection)
    old = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    new = int(datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp())
    connection.s.cache.add_message("0", "100", old, 1, {"\\Seen"}, {})
    connection.s.config.check_login = lambda user, password: True
    calls = 0
    async def incoming(fid, window):
        nonlocal calls
        calls += 1
        if calls == 2:  # first sync is SELECT; the second is the client's refresh SEARCH
            connection.s.cache.add_message(fid, "200", new, 1, set(), {})
            return 1, []
        return 0, []
    connection.s.syncer.incremental_sync.side_effect = incoming

    def client(port):
        with imaplib.IMAP4("127.0.0.1", port) as imap:
            imap.login("user@example.com", "test-password")
            assert imap.select("INBOX")[1] == [b"1"]
            assert imap.search(None, "SINCE", "5-Oct-2026")[1] == [b"2"]
            assert imap.fetch("2", "(UID FLAGS)")[1] == [b"2 (UID 2 FLAGS ())"]

    async def run():
        listener = await asyncio.start_server(connection.s._handle, "127.0.0.1", 0)
        async with listener:
            await asyncio.wait_for(asyncio.to_thread(client, listener.sockets[0].getsockname()[1]), 10)
    asyncio.run(run())


def test_failed_select_leaves_no_previous_mailbox_selected(connection):
    select(connection, [{"\\Deleted"}])
    connection.readonly = True
    dispatch(connection, b"SELECT missing")
    assert connection.selected is None
    assert not connection.readonly and not connection.snapshot and not connection.snapshot_flags
    dispatch(connection, b"STORE 1 +FLAGS (\\Seen)")
    connection.s.api.mark.assert_not_awaited()
    assert b"NO no mailbox selected" in connection.writer.data


def test_lsub_subscription_changes_use_correct_response_type(connection):
    connection.authed = True
    dispatch(connection, b'LSUB "" "Drafts"')
    assert b"* LSUB (" in connection.writer.data and b"* LIST" not in connection.writer.data
    dispatch(connection, b"UNSUBSCRIBE Drafts")
    connection.writer.data.clear()
    dispatch(connection, b'LSUB "" "Drafts"')
    assert b"* LSUB" not in connection.writer.data
    assert connection.s.cache.get_meta("subscribed:500001") == "0"
    dispatch(connection, b"SUBSCRIBE Drafts")
    dispatch(connection, b'LSUB "" "Drafts"')
    assert b"* LSUB (" in connection.writer.data


def test_folder_names_preserve_case_and_special_parent_aliases(connection):
    cache = connection.s.cache
    cache.upsert_folder("10", "Finance", "-1", "user", False, 0, 0)
    cache.upsert_folder("11", "finance", "-1", "user", False, 0, 0)
    cache.upsert_folder("500000", "Отправленные", "-1", "sent", True, 0, 0)
    cache.upsert_folder("12", "Child", "500000", "user", False, 0, 0)
    connection.s.tree.refresh()
    assert connection.s.tree.id_for("Finance") == "10"
    assert connection.s.tree.id_for("finance") == "11"
    assert connection.s.tree.id_for("FINANCE") is None
    assert connection.s.tree.name_for("12") == "Sent/Child"


def test_sasl_plain_initial_response_does_not_wait_for_challenge(connection):
    import base64
    connection.s.config.check_login = lambda user, pw: user == "user@example.com" and pw == "test-password"
    encoded = base64.b64encode(b"\x00user@example.com\x00test-password")
    dispatch(connection, b"AUTHENTICATE PLAIN " + encoded)
    assert connection.authed
    assert b"+ " not in connection.writer.data
    assert "SASL-IR" in connection.s.capabilities()


def test_cancelled_authentication_and_truncated_literal_are_recoverable(connection):
    async def run():
        connection.reader = asyncio.StreamReader()
        connection.reader.feed_data(b"*\r\n")
        await connection.dispatch([("t", b"A1 AUTHENTICATE PLAIN")])
        assert not connection.authed
        assert b"A1 BAD AUTHENTICATE cancelled" in connection.writer.data
        connection.authed = True
        connection.reader.feed_data(b"A2 APPEND Drafts {4+}\r\nbody")
        connection.reader.feed_eof()
        assert await connection.read_command() is None
        connection.s.api.save_draft.assert_not_awaited()
    asyncio.run(run())


def test_move_publishes_old_message_in_destination_and_preserves_raw_cache(connection):
    select(connection, [set()])
    raw = b"Subject: old message\r\nMessage-ID: <old@example.com>\r\n\r\nbody"
    connection.s.cache.set_raw("0", 1, raw)
    connection.s.cache.add_message("500000", "99999", 200, 1, set(), {})
    dispatch(connection, b"UID MOVE 1 Sent")
    moved = connection.s.cache.message_by_uid("500000", 2)
    assert moved.mailru_id == "100"
    assert connection.s.cache.get_raw("500000", 2) == raw
    assert moved.meta["headers"]["message-id"] == ["<old@example.com>"]
    assert connection.s.cache.counts("0") == (0, 0)


def test_move_to_same_mailbox_does_not_forget_message(connection):
    select(connection, [set()])
    dispatch(connection, b"UID MOVE 1 INBOX")
    connection.s.api.move.assert_not_awaited()
    assert connection.s.cache.message_by_uid("0", 1) is not None
    assert b"A1 NO" in connection.writer.data


def test_expunge_keeps_cached_copy_in_trash_without_deleted_flag(connection):
    select(connection, [{"\\Deleted"}])
    dispatch(connection, b"EXPUNGE")
    moved = connection.s.cache.messages("500002")
    assert len(moved) == 1 and moved[0].mailru_id == "100"
    assert "\\Deleted" not in moved[0].flags


def test_charset_failure_uses_badcharset_response(connection):
    select(connection)
    dispatch(connection, b"SEARCH CHARSET KOI8-R ALL")
    assert b"A1 NO [BADCHARSET (US-ASCII UTF-8)]" in connection.writer.data
    connection.s.syncer.incremental_sync.assert_not_awaited()


def test_missing_body_section_returns_nil_and_does_not_mark_seen(connection):
    from mailru_bridge.sync import Syncer
    select(connection, [set()])
    connection.s.cache.set_raw("0", 1, b"Subject: example\r\n\r\nbody")
    connection.s.syncer = Syncer(connection.s.api, connection.s.cache)
    dispatch(connection, b"UID FETCH 1 BODY[99]")
    assert b"BODY[99] NIL" in connection.writer.data
    connection.s.api.mark.assert_not_awaited()


def test_cached_envelope_retains_message_id_reply_to_and_encoded_words(connection):
    select(connection, [set()])
    raw = (b"Subject: =?utf-8?b?SGVsbG8=?=\r\nMessage-ID: <source@example.com>\r\n"
           b"Reply-To: Reply <reply@example.com>\r\n\r\nbody")
    connection.s.cache.set_raw("0", 1, raw)
    dispatch(connection, b"UID FETCH 1 ENVELOPE")
    assert b"<source@example.com>" in connection.writer.data
    assert b'"reply" "example.com"' in connection.writer.data


def test_auth_subscription_literal_header_search_and_sent_date_over_socket(connection):
    import base64
    select(connection, [set()])
    raw = (b"Date: Mon, 12 Oct 2026 23:50:00 -0700\r\nMessage-ID: <message@example.com>\r\n"
           b"Subject: =?utf-8?b?SGVsbG8=?=\r\nContent-Type: text/plain\r\n\r\nworld")
    connection.s.cache.set_raw("0", 1, raw)
    connection.s.config.check_login = lambda user, password: user == "user@example.com" and password == "test-password"

    async def run():
        listener = await asyncio.start_server(connection.s._handle, "127.0.0.1", 0)
        async with listener:
            reader, writer = await asyncio.open_connection('127.0.0.1', listener.sockets[0].getsockname()[1])
            await reader.readline()
            async def command(tag, text):
                writer.write(tag + b" " + text + b"\r\n")
                await writer.drain()
                lines = []
                while True:
                    line = await asyncio.wait_for(reader.readline(), 5)
                    assert line
                    lines.append(line)
                    if line.startswith(tag + b" "):
                        assert line.startswith(tag + b" OK"), line
                        return b"".join(lines)
            encoded = base64.b64encode(b"\x00user@example.com\x00test-password")
            await command(b"A1", b"AUTHENTICATE PLAIN " + encoded)
            assert b"* LSUB" in await command(b"A2", b'LSUB "" "INBOX"')
            await command(b"A3", b"SELECT INBOX")
            assert b"* SEARCH 1\r\n" in await command(b"A4", b"SEARCH SENTON 12-Oct-2026")
            phrase = b"<message@example.com>"
            wire = b"SEARCH OR (HEADER Message-ID {" + str(len(phrase)).encode() + b"+}\r\n" + phrase + b') (SUBJECT "missing")'
            assert b"* SEARCH 1\r\n" in await command(b"A5", wire)
            await command(b"A6", b"LOGOUT")
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_search_skips_externally_removed_message_and_returns_remaining_matches(connection):
    from mailru_bridge.api import NotFound
    select(connection, [set(), set()])
    async def source(fid, uid):
        if uid == 1:
            connection.s.cache.forget(fid, [uid])
            raise NotFound("removed", 404)
        return b"Subject: survivor\r\n\r\nbody"
    connection.s.syncer.ensure_raw = source
    dispatch(connection, b'SEARCH BODY "body"')
    assert b"* SEARCH 2\r\n" in connection.writer.data
    assert b"A1 OK SEARCH completed" in connection.writer.data
    assert b"EXPUNGE" not in connection.writer.data


def test_create_does_not_silently_fall_back_to_root_when_parent_is_missing(connection):
    connection.authed = True
    dispatch(connection, b"CREATE missing/child")
    connection.s.api.create_folder.assert_not_awaited()
    assert b"NO parent mailbox does not exist" in connection.writer.data


def test_rename_updates_parent_and_rejects_system_mailboxes(connection):
    connection.authed = True
    dispatch(connection, b"RENAME " + encode("Письма") + b" Sent/Child")
    connection.s.api.rename_folder.assert_awaited_once_with("9", "Child", parent="500000")
    connection.s.api.rename_folder.reset_mock()
    dispatch(connection, b"RENAME Sent Other")
    connection.s.api.rename_folder.assert_not_awaited()
    assert b"NO system mailbox cannot be renamed" in connection.writer.data
