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
    for fid, name in [("0", "INBOX"), ("500000", "Sent"), ("500001", "Drafts"), ("9", "Письма")]:
        cache.upsert_folder(fid, name, "-1", "user", False, 0, 0)
        cache.set_meta(f"deep:{fid}", "1")
    api = SimpleNamespace(**{name: AsyncMock() for name in
                            ("move", "remove", "mark", "send", "save_draft", "create_folder",
                             "delete_folder", "rename_folder")})
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


def test_modified_utf7_select_and_namespace(connection):
    connection.authed = True
    dispatch(connection, b'SELECT "' + encode("Письма") + b'"')
    assert connection.selected == "9"
    dispatch(connection, b"NAMESPACE")
    assert b'NAMESPACE (("" "/")) NIL NIL' in connection.writer.data
    assert "UIDPLUS" not in connection.s.capabilities()


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
    dispatch(connection, b'SEARCH SUBJECT "hello"')
    assert b"A1 BAD unsupported SEARCH predicate SUBJECT" in connection.writer.data


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
