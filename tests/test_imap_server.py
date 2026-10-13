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
