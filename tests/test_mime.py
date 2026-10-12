from mailru_bridge.mime import Message, body_lines, decode_text, normalize, parse_params


SAMPLE = (
    b"From: Alice <a@example.com>\n"
    b"To: b@example.com\n"
    b"Subject: =?utf-8?B?0J/RgNC40LLQtdGC?=\n"
    b'Content-Type: multipart/mixed; boundary="BB"\n\n'
    b"preamble\n"
    b"--BB\nContent-Type: text/plain; charset=utf-8\n\nhello body\n"
    b'--BB\nContent-Type: application/pdf; name="x.pdf"\n'
    b"Content-Transfer-Encoding: base64\n\nAAAA\n"
    b"--BB--\n"
)


def test_normalize_to_crlf():
    assert normalize(b"a\nb\r\nc\rd") == b"a\r\nb\r\nc\r\nd"
    already = b"a\r\nb\r\n"
    assert normalize(already) is already


def test_multipart_tree_and_sections():
    m = Message(SAMPLE)
    assert m.root.ctype == b"multipart/mixed"
    assert len(m.root.children) == 2
    assert m.section_bytes("1") == b"hello body"
    assert m.section_bytes("2") == b"AAAA"
    assert b"Subject" in m.section_bytes("HEADER")
    assert m.section_bytes("1.MIME").startswith(b"Content-Type: text/plain")


def test_header_fields_filter():
    m = Message(SAMPLE)
    only_from = m.section_bytes("HEADER.FIELDS (FROM)")
    assert only_from.startswith(b"From:")
    assert b"Subject" not in only_from
    not_from = m.section_bytes("HEADER.FIELDS.NOT (FROM TO)")
    assert b"From:" not in not_from
    assert b"Subject:" in not_from


def test_decode_encoded_word():
    assert decode_text(b"=?utf-8?B?0J/RgNC40LLQtdGC?=") == "Привет"


def test_parse_params_quoted_and_2231():
    head, params = parse_params(b'text/plain; charset="utf-8"; name=a.txt')
    assert head == b"text/plain"
    assert params["charset"] == b"utf-8"
    assert params["name"] == b"a.txt"
    _, p2 = parse_params(b"application/x; name*=utf-8''%E2%9C%93.bin")
    assert b"%E2%9C%93.bin" in p2["name"]


def test_body_lines():
    assert body_lines(b"a\r\nb\r\n") == 2
    assert body_lines(b"a\r\nb") == 2
    assert body_lines(b"") == 0
