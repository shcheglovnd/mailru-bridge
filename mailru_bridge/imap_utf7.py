"""Modified UTF-7 for IMAP mailbox names (RFC 3501 §5.1.3).

IMAP mailbox names travel as modified UTF-7: ASCII printables pass through, ``&`` is ``&-``, and any
other run is base64 of its UTF-16BE bytes between ``&`` and ``-`` with ``/`` replaced by ``,`` and
padding dropped. Mail.ru folder names are often Cyrillic, so both directions are needed.
"""

from __future__ import annotations

import base64


def encode(name: str) -> bytes:
    out = bytearray()
    i = 0
    n = len(name)
    while i < n:
        ch = name[i]
        if ch == "&":
            out += b"&-"
            i += 1
        elif 0x20 <= ord(ch) <= 0x7E:
            out += ch.encode("ascii")
            i += 1
        else:
            j = i
            while j < n and not (0x20 <= ord(name[j]) <= 0x7E):
                j += 1
            chunk = name[i:j].encode("utf-16-be")
            b64 = base64.b64encode(chunk).rstrip(b"=").replace(b"/", b",")
            out += b"&" + b64 + b"-"
            i = j
    return bytes(out)


def decode(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("ascii", "replace")
    out: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == ord("&"):
            j = data.find(b"-", i + 1)
            if j == -1:
                j = n
            seg = data[i + 1 : j]
            if seg == b"":
                out.append("&")
            else:
                b64 = seg.replace(b",", b"/")
                b64 += b"=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(chr(c))
            i += 1
    return "".join(out)
