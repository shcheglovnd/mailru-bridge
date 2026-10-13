from email.message import EmailMessage

from mailru_bridge.config import hash_password, verify_password
from mailru_bridge.service import build_outgoing
from mailru_bridge.config import BridgeConfig

import pytest


def test_password_hash_roundtrip():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h)
    assert not verify_password("wrong", h)
    assert h != hash_password("hunter2")  # random salt


def _msg() -> bytes:
    m = EmailMessage()
    m["From"] = "you@example.com"
    m["To"] = "alice@example.com, Bob <bob@example.com>"
    m["Cc"] = "carol@example.com"
    m["Subject"] = "hi"
    m.set_content("text body")
    m.add_alternative("<p>html body</p>", subtype="html")
    m.add_attachment(b"DATA", maintype="application", subtype="octet-stream", filename="f.bin")
    return m.as_bytes()


def test_build_outgoing_routes_recipients_from_envelope():
    raw = _msg()
    rcpts = ["alice@example.com", "bob@example.com", "carol@example.com", "dave@example.com"]
    out = build_outgoing(raw, "you@example.com", rcpts, "you@example.com")
    assert out.sender == "you@example.com"
    assert "alice@example.com" in " ".join(out.to)
    assert any("bob@example.com" in a for a in out.to)
    assert any("carol@example.com" in a for a in out.cc)
    assert out.bcc == ["dave@example.com"]
    assert out.subject == "hi"
    assert out.text.strip() == "text body"
    assert "html body" in out.html
    assert len(out.attachments) == 1
    assert out.attachments[0].filename == "f.bin"
    assert out.attachments[0].content == b"DATA"


def test_build_outgoing_plaintext_only():
    m = EmailMessage()
    m["From"] = "you@example.com"
    m["To"] = "x@y.ru"
    m["Subject"] = "s"
    m.set_content("only text")
    out = build_outgoing(m.as_bytes(), "you@example.com", ["x@y.ru"], "you@example.com")
    assert out.text.strip() == "only text"
    assert out.html is None
    assert out.to == ["x@y.ru"]


def test_quoted_comma_in_display_name_preserves_recipient():
    m = EmailMessage()
    m["To"] = '"Example, Alice" <alice@example.com>, other@example.com'
    m["Cc"] = '"Example, Bob" <bob@example.com>'
    m.set_content("body")
    out = build_outgoing(m.as_bytes(), "user@example.com",
                         ["alice@example.com", "bob@example.com", "hidden@example.com"], "user@example.com")
    assert out.to == ['"Example, Alice" <alice@example.com>']
    assert out.cc == ['"Example, Bob" <bob@example.com>']
    assert out.bcc == ["hidden@example.com"]
    assert "other@example.com" not in repr(out)


def test_attached_email_is_preserved_and_not_used_as_message_body():
    inner = EmailMessage()
    inner["Subject"] = "attached subject"
    inner.set_content("attached body")
    outer = EmailMessage()
    outer.set_content("outer body")
    outer.add_attachment(inner, filename="forwarded.eml")
    out = build_outgoing(outer.as_bytes(), "user@example.com", ["alice@example.com"], "user@example.com")
    assert out.text.strip() == "outer body"
    assert len(out.attachments) == 1
    assert out.attachments[0].content_type == "message/rfc822"
    assert b"Subject: attached subject" in out.attachments[0].content
    assert b"attached body" in out.attachments[0].content


def test_inline_image_without_disposition_is_preserved():
    raw = (b'Content-Type: multipart/related; boundary="BB"\r\n\r\n'
           b'--BB\r\nContent-Type: text/html\r\n\r\n<img src="cid:image1">\r\n'
           b'--BB\r\nContent-Type: image/png\r\nContent-ID: <image1>\r\n'
           b'Content-Transfer-Encoding: base64\r\n\r\nREFUQQ==\r\n--BB--\r\n')
    out = build_outgoing(raw, "user@example.com", ["alice@example.com"], "user@example.com")
    assert out.attachments[0].content == b"DATA"
    assert out.attachments[0].content_id == "image1"


@pytest.mark.parametrize("content_type", ["multipart/signed", "multipart/encrypted",
                                        "application/pkcs7-mime", "application/x-pkcs7-mime"])
def test_protected_message_format_rejected_before_json_conversion(content_type):
    from mailru_bridge.outgoing import UnsupportedMessage
    raw = (f"From: user@example.com\r\nTo: recipient@example.com\r\n"
           f"Content-Type: {content_type}\r\n\r\nprotected body").encode()
    with pytest.raises(UnsupportedMessage, match="raw-message submission"):
        build_outgoing(raw, "user@example.com", ["recipient@example.com"], "user@example.com")


def test_protected_file_attachment_can_still_be_sent_as_an_attachment():
    msg = EmailMessage()
    msg.set_content("see attached file")
    msg.add_attachment(b"PROTECTED FILE", maintype="application", subtype="pkcs7-mime", filename="file.p7m")
    outgoing = build_outgoing(msg.as_bytes(), "user@example.com", ["recipient@example.com"], "user@example.com")
    assert outgoing.attachments[0].content == b"PROTECTED FILE"


@pytest.mark.parametrize("kwargs", [{"tls_cert": "cert.pem"}, {"tls_key": "key.pem"},
                                     {"poll_seconds": 0}, {"sync_window": -1},
                                     {"initial_depth": -1}, {"imap_port": 0},
                                     {"smtp_port": 65536}, {"smtp_port": 1143}])
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        BridgeConfig(**kwargs).validate()


def test_nonascii_username_does_not_crash_login_check():
    cfg = BridgeConfig(username="user@example.com", password_hash=hash_password("password"))
    assert not cfg.check_login("пользователь@example.com", "password")
