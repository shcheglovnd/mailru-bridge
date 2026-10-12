from email.message import EmailMessage

from mailru_bridge.config import hash_password, verify_password
from mailru_bridge.service import build_outgoing


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
