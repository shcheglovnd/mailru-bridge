from datetime import datetime, timezone

import pytest

from mailru_bridge.cache import CachedMessage
from mailru_bridge.imap_server import _split_astrings, parse_sequence_set
from mailru_bridge.search import compile_search


def matches(criteria: bytes, when: str, flags=()):
    instant = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
    message = CachedMessage(1, "100", int(instant.timestamp()), 1, set(flags), False, {})
    predicate = compile_search(_split_astrings([("t", criteria)]), _split_astrings,
                               lambda spec, by_uid: parse_sequence_set(spec, [1], 1))
    return predicate(message, 1)


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
