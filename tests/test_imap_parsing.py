from mailru_bridge.imap_server import (
    _parse_fetch_items,
    _split_astrings,
    _wildcard_to_regex,
    parse_sequence_set,
)
from mailru_bridge.imap_utf7 import decode, encode


def test_utf7_roundtrip():
    for name in ["INBOX", "Sent", "Входящие", "A/Б/в", "tom & jerry", "Pokémon"]:
        assert decode(encode(name)) == name


def test_utf7_known_encoding():
    # RFC 3501 example style: & becomes &-
    assert encode("a&b") == b"a&-b"


def test_sequence_set_ranges():
    assert parse_sequence_set("1:3", [], 5) == [(1, 3)]
    assert parse_sequence_set("1,3,5", [], 5) == [(1, 1), (3, 3), (5, 5)]
    assert parse_sequence_set("2:*", [], 9) == [(2, 9)]
    assert parse_sequence_set("*", [], 7) == [(7, 7)]


def test_fetch_items_macros():
    assert _parse_fetch_items("FAST") == ["FLAGS", "INTERNALDATE", "RFC822.SIZE"]
    assert "ENVELOPE" in _parse_fetch_items("ALL")


def test_fetch_items_list_with_sections():
    items = _parse_fetch_items("(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO)] BODY[1]<0.100>)")
    assert "UID" in items
    assert "FLAGS" in items
    assert any(i.startswith("BODY.PEEK[HEADER.FIELDS") for i in items)
    assert any("<0.100>" in i for i in items)


def test_split_astrings_quoted_and_literal():
    tokens = [("t", b'"a b" plain '), ("l", b"lit data"), ("t", b"")]
    out = _split_astrings(tokens)
    assert out[0] == "a b"
    assert out[1] == "plain"
    assert out[2] == b"lit data"


def test_wildcard_to_regex():
    assert _wildcard_to_regex("*").match("Anything/Here")
    assert _wildcard_to_regex("A/%").match("A/B")
    assert not _wildcard_to_regex("A/%").match("A/B/C")
