"""Shared MIME conversion for SMTP submission and IMAP draft APPEND."""

from __future__ import annotations

import email
import re
from email.headerregistry import Address
from email.policy import default as default_policy, SMTP
from email.utils import getaddresses, parseaddr

from .api import Attachment, OutgoingMessage


class UnsupportedMessage(ValueError):
    """A MIME format that the API's structured composer cannot preserve."""


def _addresses(msg, header: str) -> list[tuple[str, str]]:
    return [(name, addr) for name, addr in getaddresses(
        [str(value) for value in msg.get_all(header, [])]
    ) if addr]


def _formatted(name: str, addr: str) -> str:
    return str(Address(display_name=name, addr_spec=addr)) if name else addr


def build_outgoing(raw: bytes, mail_from: str, rcpts: list[str], default_from: str) -> OutgoingMessage:
    """Use the SMTP envelope for delivery, preserving correctly quoted header names."""
    msg = email.message_from_bytes(raw, policy=default_policy)
    header_to = {addr.lower(): _formatted(name, addr) for name, addr in _addresses(msg, "To")}
    header_cc = {addr.lower(): _formatted(name, addr) for name, addr in _addresses(msg, "Cc")}
    to, cc, bcc = [], [], []
    for recipient in dict.fromkeys(rcpts):
        bare = parseaddr(recipient)[1].lower()
        if bare in header_to:
            to.append(header_to[bare])
        elif bare in header_cc:
            cc.append(header_cc[bare])
        else:
            bcc.append(recipient)

    text = html = None
    attachments: list[Attachment] = []

    def visit(part) -> None:
        nonlocal text, html
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        if disp != "attachment" and ctype in {
            "multipart/signed", "multipart/encrypted", "application/pkcs7-mime", "application/x-pkcs7-mime",
        }:
            raise UnsupportedMessage("PGP/MIME and S/MIME require raw-message submission, which the Mail.ru API does not provide")
        filename = part.get_filename()
        cid = str(part.get("Content-ID", "")).strip().strip("<>") or None
        is_attachment = (
            disp == "attachment" or filename is not None or ctype == "message/rfc822"
            or (not part.is_multipart() and ctype not in ("text/plain", "text/html"))
            or (disp == "inline" and cid is not None)
        )
        if is_attachment:
            if ctype == "message/rfc822" and isinstance(part.get_payload(), list):
                payload = b"\r\n".join(p.as_bytes(policy=SMTP) for p in part.get_payload())
            elif part.is_multipart():
                payload = part.as_bytes(policy=SMTP)
            else:
                payload = part.get_payload(decode=True) or b""
            attachments.append(Attachment(
                filename=filename or ("message.eml" if ctype == "message/rfc822" else "attachment"),
                content=payload, content_type=ctype,
                content_id=cid if disp != "attachment" else None,
            ))
        elif part.is_multipart():
            for child in part.iter_parts():
                visit(child)
        elif ctype == "text/plain" and text is None:
            text = part.get_content()
        elif ctype == "text/html" and html is None:
            html = part.get_content()

    visit(msg)
    if text is None and html is None:
        text = ""
    priority = str(msg.get("X-Priority", "")).strip()
    reply_ids = re.findall(r"<[^<>\s]+>", str(msg.get("In-Reply-To", "")))
    return OutgoingMessage(
        sender=str(msg.get("From") or mail_from or default_from),
        to=to, cc=cc, bcc=bcc, subject=str(msg.get("Subject", "")),
        text=text, html=html, attachments=attachments,
        priority=1 if priority.startswith(("1", "2")) else 5 if priority.startswith(("4", "5")) else 3,
        receipt=bool(msg.get("Disposition-Notification-To")),
        in_reply_to=reply_ids[-1] if reply_ids else None,
    )


def build_draft(raw: bytes, default_from: str) -> OutgoingMessage:
    msg = email.message_from_bytes(raw, policy=default_policy)
    rcpts = [addr for header in ("To", "Cc", "Bcc") for _, addr in _addresses(msg, header)]
    return build_outgoing(raw, default_from, rcpts, default_from)
