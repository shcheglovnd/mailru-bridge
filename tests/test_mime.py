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
    assert p2["name*"] == b"utf-8''%E2%9C%93.bin"


def test_body_lines():
    assert body_lines(b"a\r\nb\r\n") == 2
    assert body_lines(b"a\r\nb") == 2
    assert body_lines(b"") == 0


def test_boundary_prefix_inside_body_is_not_a_delimiter():
    raw = (b'Content-Type: multipart/mixed; boundary="BB"\r\n\r\n'
           b'--BB\r\nContent-Type: text/plain\r\n\r\nhello\r\n--BBextra\r\nmore\r\n--BB--\r\n')
    msg = Message(raw)
    assert len(msg.root.children) == 1
    assert msg.section_bytes("1") == b"hello\r\n--BBextra\r\nmore"


def test_empty_mime_headers_and_missing_closing_boundary():
    raw = (b'Content-Type: multipart/mixed; boundary="BB"\r\n\r\n'
           b'--BB\r\n\r\nbody\r\n')
    msg = Message(raw)
    assert msg.section_bytes("1") == b"body\r\n"
    assert msg.section_bytes("1.MIME") == b"\r\n"


def test_nonexistent_nested_sections_are_not_aliases_for_plain_body():
    msg = Message(b"Content-Type: text/plain\r\n\r\nbody")
    assert msg.section_bytes("1") == b"body"
    assert msg.section_bytes("1.1") is None
    assert msg.section_bytes("1.HEADER") is None
    assert msg.section_bytes("0") is None


def test_forwarded_message_sections_keep_encapsulated_headers():
    inner = b"From: Alice <alice@example.com>\r\nContent-Type: text/plain\r\n\r\ninner body"
    msg = Message(b"Content-Type: message/rfc822\r\n\r\n" + inner)
    assert msg.section_bytes("1") == inner
    assert msg.section_bytes("1.HEADER").startswith(b"From:")
    assert msg.section_bytes("1.1") == b"inner body"
    raw = (b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
           b"--B\r\nContent-Type: text/plain\r\n\r\nouter\r\n"
           b"--B\r\nContent-Type: message/rfc822\r\n\r\n" + inner + b"\r\n--B--\r\n")
    msg = Message(raw)
    assert msg.section_bytes("2") == inner
    assert msg.section_bytes("2.1") == b"inner body"
    assert msg.section_bytes("2.HEADER").startswith(b"From:")


def test_rfc2231_continuations_keep_charset_and_segment_names():
    _, params = parse_params(b"attachment; filename*0*=utf-8''%E2%9C; filename*1*=%93.bin")
    assert params == {"filename*0*": b"utf-8''%E2%9C", "filename*1*": b"%93.bin"}
