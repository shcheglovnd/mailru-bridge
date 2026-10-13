"""Compile IMAP SEARCH predicates with lazy, short-circuited source access."""

import email
import re
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable

from .mime import decoded_headers

MONTHS = {name: number for number, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


class UnsupportedCharset(ValueError):
    pass


def _text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _search_date(value) -> date:
    match = re.fullmatch(r"([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})", _text(value))
    if not match or match[2].upper() not in MONTHS:
        raise ValueError("invalid SEARCH date")
    try:
        return date(int(match[3]), MONTHS[match[2].upper()], int(match[1]))
    except ValueError:
        raise ValueError("invalid SEARCH date") from None


class SearchData:
    def __init__(self, message, seq, load_raw):
        self.message, self.seq, self.load_raw = message, seq, load_raw
        self._raw = None
        self._headers = message.meta.get("headers")
        self._body = None

    async def raw(self):
        if self._raw is None:
            if self.load_raw is None:
                raise ValueError("message source required for SEARCH")
            self._raw = await self.load_raw()
        return self._raw

    async def headers(self):
        if self._headers is None:
            self._headers = decoded_headers(await self.raw())
        return self._headers

    async def header(self, name):
        name = name.lower()
        if self._headers is not None:
            return self._headers.get(name, [])
        meta = self.message.meta
        if name == "subject" and "subject" in meta:
            return [meta["subject"]]
        if name in {"from", "to", "cc", "bcc"} and name in meta:
            return [f"{display} <{addr}>" if display else addr for display, addr in meta[name]]
        return (await self.headers()).get(name, [])

    async def body(self):
        if self._body is None:
            msg = email.message_from_bytes(await self.raw(), policy=default_policy)
            parts = []
            for part in msg.walk():
                if part.get_content_maintype() != "text":
                    continue
                payload = part.get_payload(decode=True) or b""
                try:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
                except LookupError:
                    parts.append(payload.decode("utf-8", "replace"))
            self._body = "\n".join(parts).casefold()
        return self._body


@dataclass
class Rule:
    cost: int
    test: Callable[[SearchData], Awaitable[bool]]


def _simple(test):
    async def match(data):
        return test(data)
    return Rule(0, match)


def _combine(rules, either=False):
    rules = sorted(rules, key=lambda rule: rule.cost)
    async def match(data):
        for rule in rules:
            result = await rule.test(data)
            if either and result:
                return True
            if not either and not result:
                return False
        return not either
    return Rule(sum(rule.cost for rule in rules), match)


def compile_search(args: list, split, ranges):
    args = deque(args)
    if args and _text(args[0]).upper() == "CHARSET":
        args.popleft()
        if not args or _text(args.popleft()).upper() not in {"UTF-8", "US-ASCII"}:
            raise UnsupportedCharset("unsupported SEARCH charset")

    def argument(stream):
        if not stream or isinstance(stream[0], list):
            raise ValueError("missing SEARCH argument")
        return _text(stream.popleft())

    def parse(stream, depth=0):
        if not stream or depth > 64:
            raise ValueError("missing or excessively nested SEARCH predicate")
        token = stream.popleft()
        if isinstance(token, list):
            if not token:
                raise ValueError("empty SEARCH group")
            return conjunction(deque(token), depth + 1)
        key = _text(token).upper()
        if key in {"ALL", "OLD"}:
            return _simple(lambda data: True)
        if key in {"RECENT", "NEW"}:
            return _simple(lambda data: False)
        if key == "NOT":
            child = parse(stream, depth + 1)
            async def negate(data):
                return not await child.test(data)
            return Rule(child.cost, negate)
        if key == "OR":
            return _combine([parse(stream, depth + 1), parse(stream, depth + 1)], either=True)
        if key in {"BEFORE", "ON", "SINCE", "SENTBEFORE", "SENTON", "SENTSINCE"}:
            wanted = _search_date(argument(stream))
            sent = key.startswith("SENT")
            op = key[4:] if sent else key
            async def matches_date(data):
                if sent:
                    values = await data.header("date")
                    if not values:
                        return False
                    try:
                        actual = parsedate_to_datetime(values[0]).date()
                    except (ValueError, TypeError, OverflowError):
                        return False
                else:
                    actual = datetime.fromtimestamp(data.message.date, timezone.utc).date()
                return actual < wanted if op == "BEFORE" else actual == wanted if op == "ON" else actual >= wanted
            return Rule(1 if sent else 0, matches_date)
        flags = {"SEEN": "\\Seen", "FLAGGED": "\\Flagged", "ANSWERED": "\\Answered",
                 "DELETED": "\\Deleted", "DRAFT": "\\Draft"}
        positive = key[2:] if key.startswith("UN") else key
        if positive in flags:
            return _simple(lambda data: (flags[positive] in data.message.flags) == (not key.startswith("UN")))
        if key in {"KEYWORD", "UNKEYWORD"}:
            flag = argument(stream).casefold()
            return _simple(lambda data: (flag in {f.casefold() for f in data.message.flags}) == (key == "KEYWORD"))
        if key in {"LARGER", "SMALLER"}:
            value = argument(stream)
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError("invalid SEARCH size")
            size = int(value)
            return _simple(lambda data: data.message.size > size if key == "LARGER" else data.message.size < size)
        if key in {"FROM", "TO", "CC", "BCC", "SUBJECT", "HEADER"}:
            name = argument(stream).lower() if key == "HEADER" else key.lower()
            wanted = argument(stream).casefold()
            async def matches_header(data):
                return any(wanted in value.casefold() for value in await data.header(name))
            cost = 0 if name in {"from", "to", "cc", "bcc", "subject"} else 1
            return Rule(cost, matches_header)
        if key in {"BODY", "TEXT"}:
            wanted = argument(stream).casefold()
            async def matches_text(data):
                if key == "TEXT" and any(wanted in value.casefold() for values in (await data.headers()).values() for value in values):
                    return True
                return wanted in await data.body()
            return Rule(2, matches_text)
        if key == "UID" or re.fullmatch(r"[0-9*:,]+", key):
            by_uid = key == "UID"
            selected = ranges(argument(stream) if by_uid else key, by_uid)
            return _simple(lambda data: any(lo <= (data.message.uid if by_uid else data.seq) <= hi for lo, hi in selected))
        raise ValueError(f"unsupported SEARCH predicate {key}")

    def conjunction(stream, depth=0):
        rules = []
        while stream:
            rules.append(parse(stream, depth))
        return _combine(rules)

    if not args:
        raise ValueError("missing SEARCH criteria")
    root = conjunction(args)
    async def matches(message, seq, load_raw=None):
        return await root.test(SearchData(message, seq, load_raw))
    return matches
