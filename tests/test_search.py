from datetime import datetime, timezone
import asyncio
from unittest.mock import AsyncMock

import pytest

from mailru_bridge.cache import CachedMessage
from mailru_bridge.imap_server import _split_astrings, parse_sequence_set
from mailru_bridge.search import compile_search


def matches(criteria: bytes, when: str, flags=()):
    instant = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
    message = CachedMessage(1, "100", int(instant.timestamp()), 1, set(flags), False, {})
    predicate = compile_search(_split_astrings([("t", criteria)]), _split_astrings,
                               lambda spec, by_uid: parse_sequence_set(spec, [1], 1))
    return asyncio.run(predicate(message, 1))


@pytest.mark.parametrize("criteria,when,expected", [
    (b"SINCE 12-Oct-2026", "2026-10-12T00:00:00", True),
    (b"SINCE 12-Oct-2026", "2026-10-11T23:59:59", False),
    (b"BEFORE 12-Oct-2026", "2026-10-11T23:59:59", True),
    (b"BEFORE 12-Oct-2026", "2026-10-12T00:00:00", False),
    (b"ON 12-Oct-2026", "2026-10-12T23:59:59", True),
    (b"ON 12-Oct-2026", "2026-10-13T00:00:00", False),
    (b'ON "12-oCt-2026"', "2026-10-12T00:00:00", True),
    (b"NOT BEFORE 12-Oct-2026", "2026-10-13T00:00:00", True),
    (b"(SINCE 12-Oct-2026 BEFORE 14-Oct-2026)", "2026-10-13T00:00:00", True),
    (b"OR SINCE 12-Oct-2026 UNSEEN", "2026-09-01T00:00:00", True),
    (b"OR OR SINCE 12-Oct-2026 UNSEEN FLAGGED", "2026-09-01T00:00:00", True),
    (b"CHARSET UTF-8 SINCE 1-Oct-2026", "2026-10-13T00:00:00", True),
    (b"ON 29-Feb-2024", "2024-02-29T12:00:00", True),
])
def test_received_date_queries(criteria, when, expected):
    assert matches(criteria, when) is expected


@pytest.mark.parametrize("criteria", [b"SINCE", b"SINCE invalid", b"SINCE 31-Sep-2026",
                                       b"ON 29-Feb-2025", b"ON 0-Jan-2026", b"BEFORE 1-Xxx-2026"])
def test_invalid_date_queries_are_rejected(criteria):
    with pytest.raises(ValueError):
        matches(criteria, "2026-10-13T00:00:00")


def query(criteria, meta=None, flags=(), raw=b"", size=100):
    message = CachedMessage(7, "100", 1791853200, size, set(flags), False, meta or {})
    load = AsyncMock(return_value=raw)
    predicate = compile_search(_split_astrings([("t", criteria)]), _split_astrings,
                               lambda spec, by_uid: parse_sequence_set(spec, [7], 7))
    result = asyncio.run(predicate(message, 1, load))
    return result, load


@pytest.mark.parametrize("criteria", [b'SUBJECT "invoice"', b'FROM "alice@example.com"',
                                       b'TO "Bob"', b'CC "carol@example.com"', b'BCC "hidden@example.com"',
                                       b'HEADER Subject "invoice"'])
def test_known_headers_search_without_downloading_source(criteria):
    result, load = query(criteria, {"subject": "An Invoice", "from": [("Alice", "alice@example.com")],
                                   "to": [("Bob", "bob@example.com")], "cc": [("", "carol@example.com")],
                                   "bcc": [("", "hidden@example.com")]})
    assert result
    load.assert_not_awaited()


def test_message_id_header_uses_cached_headers():
    result, load = query(b'HEADER Message-ID "<target@example.com>"',
                         {"headers": {"message-id": ["<target@example.com>"]}})
    assert result
    load.assert_not_awaited()


def test_custom_header_presence_distinguishes_empty_from_missing():
    assert query(b'HEADER X-Test ""', {"headers": {"x-test": [""]}})[0]
    assert not query(b'HEADER X-Test ""', {"headers": {}})[0]


def test_sent_dates_use_header_calendar_date_and_preserve_timezone():
    # UTC arrival is Oct 13; Date header's local calendar day is Oct 12.
    meta = {"headers": {"date": ["Mon, 12 Oct 2026 23:50:00 -0700"]}}
    assert query(b"SENTON 12-Oct-2026", meta)[0]
    assert not query(b"SENTON 13-Oct-2026", meta)[0]
    assert query(b"ON 13-Oct-2026", meta)[0]
    assert query(b"SENTBEFORE 13-Oct-2026", meta)[0]
    assert query(b"SENTSINCE 12-Oct-2026", meta)[0]


@pytest.mark.parametrize("value", [None, "invalid"])
def test_sent_date_missing_or_malformed_does_not_match(value):
    assert not query(b"SENTSINCE 1-Oct-2026", {"headers": {"date": [value] if value else []}})[0]


def test_body_text_decodes_transfer_encoding_and_loads_only_once():
    raw = (b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
           b"SGVsbG8gd29ybGQ=\r\n")
    result, load = query(b'BODY "hello" BODY "WORLD"', raw=raw)
    assert result
    load.assert_awaited_once()


def test_text_search_includes_decoded_header_fields():
    result, load = query(b'TEXT "hello"', raw=b"Subject: =?utf-8?b?SGVsbG8=?=\r\n\r\nother body\r\n")
    assert result
    load.assert_awaited_once()


def test_expensive_search_predicates_short_circuit_after_cheap_filters():
    result, load = query(b'BODY "secret" UID 8')
    assert not result
    load.assert_not_awaited()
    result, load = query(b'OR BODY "secret" SEEN', flags={"\\Seen"})
    assert result
    load.assert_not_awaited()


@pytest.mark.parametrize("criteria,expected", [(b"LARGER 99", True), (b"LARGER 100", False),
                                                (b"SMALLER 101", True), (b"SMALLER 100", False)])
def test_size_predicates(criteria, expected):
    assert query(criteria, size=100)[0] is expected


def test_nested_group_with_literal_search_argument():
    tokens = [("t", b"OR (SUBJECT "), ("l", "Привет (test)".encode()), ("t", b') (BODY "unused")')]
    predicate = compile_search(_split_astrings(tokens), _split_astrings, lambda spec, by_uid: [])
    msg = CachedMessage(1, "100", 1, 1, set(), False, {"subject": "Привет (test)"})
    load = AsyncMock(side_effect=AssertionError("metadata match should avoid body download"))
    assert asyncio.run(predicate(msg, 1, load))
    load.assert_not_awaited()
