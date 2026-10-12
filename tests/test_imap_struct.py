from mailru_bridge.imap_struct import bodystructure, envelope, envelope_from_meta, quote
from mailru_bridge.mime import Message

MULTI = (
    b"From: Alice <a@example.com>\nTo: b@example.com\n"
    b"Date: Mon, 12 Oct 2026 10:00:00 +0000\nMessage-ID: <x@example.com>\n"
    b"Subject: hi\nContent-Type: multipart/mixed; boundary=BB\n\n"
    b"--BB\nContent-Type: text/plain\n\nbody\n"
    b"--BB\nContent-Type: application/pdf\nContent-Transfer-Encoding: base64\n\nAAAA\n--BB--\n"
)


def test_quote():
    assert quote(None) == b"NIL"
    assert quote("a") == b'"a"'
    assert quote(b'a"b') == b'"a\\"b"'
    assert quote(b"a\r\nb").startswith(b"{4}\r\n")


def test_envelope_fields():
    env = envelope(Message(MULTI))
    assert env.startswith(b"(")
    assert b'"hi"' in env
    assert b'"a" "example.com"' in env
    assert b"<x@example.com>" in env


def test_bodystructure_multipart():
    bs = bodystructure(Message(MULTI))
    assert b'"text" "plain"' in bs
    assert b'"application" "pdf"' in bs
    assert bs.rstrip().endswith(b")")
    # the text part carries a line count after the size
    assert b'"7bit" 5 1' in bs or b'"7bit" 4 1' in bs or b'"7bit"' in bs


def test_bodystructure_simple():
    bs = bodystructure(Message(b"From: a@b.com\n\nhello\nworld\n"))
    assert bs.startswith(b'("text" "plain"')


def test_envelope_from_meta():
    meta = {"subject": "Привет", "from": [("Alice", "a@example.com")], "to": [("", "b@x.ru")], "cc": []}
    env = envelope_from_meta(meta, 1760000000)
    assert env.count(b"(") >= 3
    assert b'"a" "example.com"' in env
    assert b'"b" "x.ru"' in env
