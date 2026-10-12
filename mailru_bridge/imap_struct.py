"""IMAP ``ENVELOPE`` and ``BODYSTRUCTURE`` for a parsed :class:`~mailru_bridge.mime.Message`.

These are the structured FETCH items a client reads before deciding what sections to download, so
they must match the message's real octets. Everything is emitted as the IMAP wire form: a
parenthesised list of quoted strings, ``NIL``, and numbers (RFC 3501 §7.4.2 / §9).
"""

from __future__ import annotations

from email.utils import formatdate, getaddresses, parsedate_to_datetime

from .mime import CRLF, Message, Part, body_lines, decode_text, field_value


def quote(value) -> bytes:
    """IMAP quoted-string (or literal for values with CR/LF), or ``NIL`` for ``None``."""
    if value is None:
        return b"NIL"
    if isinstance(value, str):
        value = value.encode("utf-8")
    if b"\r" in value or b"\n" in value:
        return b"{%d}\r\n%s" % (len(value), value)
    escaped = value.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + escaped + b'"'


def _list(items: list[bytes]) -> bytes:
    return b"(" + b" ".join(items) + b")" if items else b"NIL"


# -- ENVELOPE ---------------------------------------------------------------------------------


def _addr_list(raw: bytes | None) -> bytes:
    """IMAP address-list: ``((name adl mailbox host) ...)`` or ``NIL``."""
    if not raw:
        return b"NIL"
    text = raw.decode("utf-8", "replace")
    out = []
    for display, addr in getaddresses([text]):
        if not display and not addr:
            continue
        name = quote(decode_text(display.encode("utf-8")) if display else None)
        local, _, host = addr.partition("@")
        out.append(b"(" + name + b" NIL " + quote(local or None) + b" " + quote(host or None) + b")")
    return b"(" + b"".join(out) + b")" if out else b"NIL"


def envelope(msg: Message) -> bytes:
    fields = msg.root.fields
    date = field_value(fields, "date")
    subject_raw = field_value(fields, "subject")
    subject = quote(decode_text(subject_raw)) if subject_raw is not None else b"NIL"
    from_ = _addr_list(field_value(fields, "from"))
    sender = _addr_list(field_value(fields, "sender")) if field_value(fields, "sender") else from_
    reply_to = _addr_list(field_value(fields, "reply-to")) if field_value(fields, "reply-to") else from_
    to = _addr_list(field_value(fields, "to"))
    cc = _addr_list(field_value(fields, "cc"))
    bcc = _addr_list(field_value(fields, "bcc"))
    in_reply_to = quote(field_value(fields, "in-reply-to"))
    message_id = quote(field_value(fields, "message-id"))
    return b"(" + b" ".join([quote(date), subject, from_, sender, reply_to, to, cc, bcc,
                             in_reply_to, message_id]) + b")"


def _addr_pairs(pairs) -> bytes:
    """IMAP address-list from stored ``[(name, email), ...]`` metadata."""
    out = []
    for name, addr in pairs or []:
        local, _, host = (addr or "").partition("@")
        out.append(b"(" + quote(name or None) + b" NIL " + quote(local or None)
                   + b" " + quote(host or None) + b")")
    return b"(" + b"".join(out) + b")" if out else b"NIL"


def envelope_from_meta(meta: dict, date: int) -> bytes:
    """ENVELOPE synthesised from cached list metadata, to avoid downloading every message.

    Fields we do not have from the list (sender/reply-to default to From; in-reply-to and
    message-id are ``NIL``) follow the same fall-backs the spec allows.
    """
    date_hdr = quote(formatdate(date, localtime=False)) if date else b"NIL"
    subject = quote(meta.get("subject") or None)
    from_ = _addr_pairs(meta.get("from"))
    to = _addr_pairs(meta.get("to"))
    cc = _addr_pairs(meta.get("cc"))
    return b"(" + b" ".join([date_hdr, subject, from_, from_, from_, to, cc,
                             b"NIL", b"NIL", b"NIL"]) + b")"


# -- BODYSTRUCTURE ----------------------------------------------------------------------------


def _params_list(params: dict[str, bytes]) -> bytes:
    if not params:
        return b"NIL"
    items = []
    for k, v in params.items():
        items.append(quote(k.encode("ascii", "replace")))
        items.append(quote(v))
    return _list(items)


def _disposition(part: Part) -> bytes:
    raw = field_value(part.fields, "content-disposition")
    if not raw:
        return b"NIL"
    from .mime import parse_params

    kind, params = parse_params(raw)
    return b"(" + quote(kind or None) + b" " + _params_list(params) + b")"


def _simple_fields(part: Part) -> list[bytes]:
    """The trailing extension fields shared by all part types: md5, disposition, lang, loc."""
    return [b"NIL", _disposition(part), b"NIL", b"NIL"]


def bodystructure(msg: Message, extensible: bool = True) -> bytes:
    return _part_structure(msg.raw, msg.root, extensible)


def _part_structure(raw: bytes, part: Part, ext: bool) -> bytes:
    if part.is_multipart:
        children = b"".join(_part_structure(raw, c, ext) for c in part.children) or b"NIL"
        pieces = [children, quote(part.subtype.encode("ascii", "replace"))]
        if ext:
            pieces += [_params_list(part.params), _disposition(part), b"NIL", b"NIL"]
        return b"(" + b" ".join(p for p in pieces if p is not None) + b")"

    body = raw[part.body_start : part.body_end]
    enc = field_value(part.fields, "content-transfer-encoding") or b"7bit"
    size = len(body)
    main = part.maintype or "text"
    sub = part.subtype or "plain"
    base = [
        quote(main.encode("ascii", "replace")),
        quote(sub.encode("ascii", "replace")),
        _params_list(part.params),
        quote(field_value(part.fields, "content-id")),
        quote(field_value(part.fields, "content-description")),
        quote(enc.strip()),
        b"%d" % size,
    ]
    if main == "text":
        base.append(b"%d" % body_lines(body))
    elif part.is_message and part.children:
        inner = part.children[0]
        base.append(envelope(_Wrap(raw, inner)))
        base.append(_part_structure(raw, inner, ext))
        base.append(b"%d" % body_lines(body))
    if ext:
        base += _simple_fields(part)
    return b"(" + b" ".join(base) + b")"


class _Wrap:
    """Adapt a sub-:class:`Part` so :func:`envelope` can read its header fields."""

    def __init__(self, raw: bytes, part: Part):
        self.raw = raw
        self.root = part
