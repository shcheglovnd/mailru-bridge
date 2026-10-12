"""Byte-exact MIME structure of a message, for IMAP ``FETCH``.

IMAP addresses parts of a message by section number (``BODY[1.2]``, ``BODY[2.HEADER]``,
``BODY[1.MIME]``) and describes it with ``BODYSTRUCTURE`` and ``ENVELOPE``. All of that is
answered from the original octets — clients compare sizes and offsets — so this module locates
every part by byte offsets instead of going through :mod:`email` (which re-serialises).

Messages are kept with CRLF line endings (:func:`normalize`); sizes and line counts refer to that
form. Header values are handled as ``bytes`` and are only decoded for the human-readable fields
(subject, address display names) that IMAP ``ENVELOPE`` expects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_tz

CRLF = b"\r\n"


def normalize(data: bytes) -> bytes:
    """Return ``data`` with every line ending as CRLF (IMAP's canonical form)."""
    n = data.count(b"\n")
    if n and data.count(b"\r\n") == n and data.count(b"\r") == n:
        return data
    return re.sub(rb"\r\n|\r|\n", b"\r\n", data)


# -- header fields ----------------------------------------------------------------------------


@dataclass
class HeaderField:
    name: str  # as written, e.g. "Content-Type"
    raw: bytes  # the whole field incl. folded continuation lines and the final CRLF
    value: bytes  # unfolded value, leading/trailing whitespace stripped


def parse_fields(block: bytes) -> list[HeaderField]:
    """Split a header block (CRLF-separated, without the trailing blank line) into fields."""
    fields: list[HeaderField] = []
    lines = block.split(CRLF)
    while lines and lines[-1] == b"":
        lines.pop()
    cur_name: str | None = None
    cur_lines: list[bytes] = []

    def flush() -> None:
        if cur_name is None:
            return
        raw = CRLF.join(cur_lines) + CRLF
        colon = cur_lines[0].find(b":")
        value = cur_lines[0][colon + 1 :] if colon >= 0 else b""
        for cont in cur_lines[1:]:
            value += b" " + cont.strip(b" \t")
        fields.append(HeaderField(cur_name, raw, value.strip(b" \t")))

    for line in lines:
        if line[:1] in (b" ", b"\t") and cur_name is not None:
            cur_lines.append(line)
            continue
        flush()
        colon = line.find(b":")
        cur_name = line[:colon].decode("latin-1").strip() if colon > 0 else ""
        cur_lines = [line]
    flush()
    return fields


def field_value(fields: list[HeaderField], name: str) -> bytes | None:
    low = name.lower()
    for f in fields:
        if f.name.lower() == low:
            return f.value
    return None


def decode_text(value: bytes | None) -> str:
    """Human-readable form of a header value, RFC 2047 encoded-words decoded."""
    if not value:
        return ""
    text = value.decode("utf-8", "replace") if _is_utf8(value) else value.decode("latin-1")
    try:
        return str(make_header(decode_header(text)))
    except Exception:  # malformed encoded-words: keep the raw text
        return text


def _is_utf8(value: bytes) -> bool:
    try:
        value.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# -- parameterised headers (Content-Type, Content-Disposition) --------------------------------

_PARAM = re.compile(rb'\s*;\s*([^\s;=]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)')


def parse_params(value: bytes) -> tuple[bytes, dict[str, bytes]]:
    """Split ``type/sub; a=b; c="d"`` into the leading token and a dict of parameters.

    Parameter names are lower-cased; values are unquoted. RFC 2231 continuations/charset
    forms (``name*0``, ``name*=utf-8''…``) are joined into the base name, best-effort.
    """
    if value is None:
        return b"", {}
    semi = value.find(b";")
    head = value[:semi].strip() if semi >= 0 else value.strip()
    params: dict[str, bytes] = {}
    for m in _PARAM.finditer(value):
        name = m.group(1).lower().decode("latin-1")
        raw = m.group(2)
        if raw[:1] == b'"':
            raw = raw[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        params[name] = raw
    # Merge RFC 2231 split parameters: foo*0, foo*1 -> foo ; foo* -> foo
    merged: dict[str, bytes] = {}
    continued: dict[str, list[tuple[int, bytes]]] = {}
    for name, raw in params.items():
        base = name
        idx = 0
        if "*" in name:
            star = name.split("*")
            base = star[0]
            if len(star) > 1 and star[1].isdigit():
                idx = int(star[1])
        if "*" in name:
            continued.setdefault(base, []).append((idx, raw))
        else:
            merged[name] = raw
    for base, parts in continued.items():
        joined = b"".join(v for _, v in sorted(parts))
        # strip a leading charset'lang' from the 2231 extended form
        m = re.match(rb"^[\w-]*'[\w-]*'(.*)$", joined)
        merged[base] = m.group(1) if m else joined
    return head, merged


# -- message tree -----------------------------------------------------------------------------


@dataclass
class Part:
    """One MIME entity, located by byte offsets into the whole (CRLF-normalised) message."""

    # offsets into the full message bytes
    header_start: int
    body_start: int
    body_end: int  # exclusive; for a multipart this spans all children incl. boundaries
    fields: list[HeaderField]
    ctype: bytes  # "text/plain", lower-cased main/sub
    params: dict[str, bytes]
    children: list["Part"] = field(default_factory=list)

    @property
    def maintype(self) -> str:
        return self.ctype.split(b"/", 1)[0].decode("latin-1").lower()

    @property
    def subtype(self) -> str:
        parts = self.ctype.split(b"/", 1)
        return (parts[1] if len(parts) > 1 else b"").decode("latin-1").lower()

    @property
    def is_multipart(self) -> bool:
        return self.maintype == "multipart"

    @property
    def is_message(self) -> bool:
        return self.ctype.lower() == b"message/rfc822"


class Message:
    """A parsed message: the raw octets plus the part tree, addressable by IMAP section."""

    def __init__(self, raw: bytes):
        self.raw = normalize(raw)
        self.root = _parse_part(self.raw, 0, len(self.raw))

    # -- sections --------------------------------------------------------------------------

    def _find(self, section: str) -> Part | None:
        """Resolve an IMAP numeric section like ``1`` or ``2.1`` to a part."""
        if not section:
            return self.root
        part = self.root
        numbers = section.split(".")
        # For a non-multipart top-level message, section "1" is the whole body.
        if not part.is_multipart and numbers and numbers[0] == "1" and len(numbers) == 1:
            if part.is_message and part.children:
                return part.children[0]
            return part
        for n in numbers:
            idx = int(n) - 1
            if part.is_message and part.children:
                part = part.children[0]
            if part.is_multipart:
                if idx < 0 or idx >= len(part.children):
                    return None
                part = part.children[idx]
            else:
                if idx != 0:
                    return None
        return part

    def section_bytes(self, spec: str) -> bytes | None:
        """Return the octets for an IMAP ``BODY[...]`` ``spec`` (without the surrounding []).

        Supports: ``""`` (whole message), ``HEADER``, ``TEXT``, ``HEADER.FIELDS (...)``,
        ``HEADER.FIELDS.NOT (...)``, ``<n>``, ``<n>.MIME``, ``<n>.HEADER``, ``<n>.TEXT``,
        and nested combinations such as ``1.2.HEADER``.
        """
        spec = spec.strip()
        upper = spec.upper()
        if spec == "":
            return self.raw
        if upper == "HEADER":
            return self._header_bytes(self.root)
        if upper == "TEXT":
            return self.raw[self.root.body_start : self.root.body_end]
        if upper.startswith("HEADER.FIELDS"):
            return self._header_fields(self.root, spec)

        # numeric section, optionally followed by a keyword
        m = re.match(r"^([\d.]+?)(?:\.(MIME|HEADER|TEXT|HEADER\.FIELDS.*|HEADER\.FIELDS\.NOT.*))?$",
                     spec, re.IGNORECASE)
        if not m:
            return None
        numeric, keyword = m.group(1), (m.group(2) or "")
        part = self._find(numeric)
        if part is None:
            return None
        kw = keyword.upper()
        if kw == "" or kw == "":
            # bare numeric section: the part's body (for message/rfc822, the encapsulated body)
            return self.raw[part.body_start : part.body_end]
        if kw == "MIME":
            return self.raw[part.header_start : part.body_start]
        if kw == "HEADER":
            inner = part.children[0] if part.is_message and part.children else part
            return self._header_bytes(inner)
        if kw == "TEXT":
            inner = part.children[0] if part.is_message and part.children else part
            return self.raw[inner.body_start : inner.body_end]
        if kw.startswith("HEADER.FIELDS"):
            inner = part.children[0] if part.is_message and part.children else part
            return self._header_fields(inner, keyword)
        return None

    def _header_bytes(self, part: Part) -> bytes:
        return self.raw[part.header_start : part.body_start]

    def _header_fields(self, part: Part, keyword: str) -> bytes:
        names = re.findall(r"[^\s()]+", keyword.split("(", 1)[1]) if "(" in keyword else []
        wanted = {n.lower() for n in names}
        negate = "NOT" in keyword.upper().split("(", 1)[0]
        out = bytearray()
        for f in part.fields:
            hit = f.name.lower() in wanted
            if hit != negate:
                out += f.raw
        out += CRLF  # the blank line terminating the (sub)header
        return bytes(out)


# -- parser -----------------------------------------------------------------------------------


def _split_header_body(data: bytes, start: int, end: int) -> int:
    """Return the offset of the body start (after the blank line), or ``end`` if none."""
    sep = data.find(b"\r\n\r\n", start, end)
    if sep < 0:
        return end
    return sep + 4


def _parse_part(data: bytes, start: int, end: int) -> Part:
    body_start = _split_header_body(data, start, end)
    header_block = data[start : body_start]
    # drop the final blank line from the field block
    fields = parse_fields(header_block[:-2] if header_block.endswith(b"\r\n\r\n") else header_block)
    ctype_raw = field_value(fields, "content-type") or b"text/plain"
    head, params = parse_params(ctype_raw)
    ctype = head.lower() or b"text/plain"
    part = Part(start, body_start, end, fields, ctype, params)

    if part.maintype == "multipart":
        boundary = params.get("boundary")
        if boundary:
            part.children = _parse_multipart(data, body_start, end, boundary)
    elif part.is_message:
        child = _parse_part(data, body_start, end)
        part.children = [child]
    return part


def _parse_multipart(data: bytes, start: int, end: int, boundary: bytes) -> list[Part]:
    delim = b"--" + boundary
    children: list[Part] = []
    # positions of each boundary line
    marks: list[int] = []
    pos = start
    while True:
        i = data.find(delim, pos, end)
        if i < 0:
            break
        # must be at line start
        if i == start or data[i - 1 : i] == b"\n":
            marks.append(i)
        pos = i + len(delim)
    for idx in range(len(marks) - 1):
        after = data.find(b"\r\n", marks[idx], end)
        if after < 0:
            continue
        if data[marks[idx] + len(delim) : marks[idx] + len(delim) + 2] == b"--":
            continue  # closing boundary
        child_start = after + 2
        child_end = marks[idx + 1]
        # the CRLF immediately before the next boundary belongs to the boundary
        if data[child_end - 2 : child_end] == b"\r\n":
            child_end -= 2
        children.append(_parse_part(data, child_start, child_end))
    return children


# -- counts -----------------------------------------------------------------------------------


def body_lines(data: bytes) -> int:
    """Number of CRLF-terminated lines in ``data`` (for IMAP body/part line counts)."""
    if not data:
        return 0
    n = data.count(CRLF)
    if not data.endswith(CRLF):
        n += 1
    return n
