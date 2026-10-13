"""Flag, internal-date and sequence SEARCH predicates."""

import re
from datetime import date, datetime, timezone

MONTHS = {name: number for number, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def _search_date(value) -> date:
    text = value.decode("ascii") if isinstance(value, bytes) else str(value)
    match = re.fullmatch(r"([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})", text)
    if not match or match[2].upper() not in MONTHS:
        raise ValueError("invalid SEARCH date")
    try:
        return date(int(match[3]), MONTHS[match[2].upper()], int(match[1]))
    except ValueError:
        raise ValueError("invalid SEARCH date") from None


def compile_search(args: list, split, ranges):
    args = list(args)
    if args and str(args[0]).upper() == "CHARSET":
        if len(args) < 2 or str(args[1]).upper() not in {"UTF-8", "US-ASCII"}:
            raise ValueError("unsupported SEARCH charset")
        args = args[2:]

    def parse(stream: list):
        if not stream:
            raise ValueError("missing SEARCH predicate")
        token = stream.pop(0)
        if isinstance(token, bytes) and token.startswith(b"(") and token.endswith(b")"):
            return conjunction(split([("t", token[1:-1])]))
        key = token.decode("utf-8") if isinstance(token, bytes) else token
        key = key.upper()
        if key == "ALL" or key == "OLD":
            return lambda cm, seq: True
        if key in {"RECENT", "NEW"}:
            return lambda cm, seq: False
        if key == "NOT":
            predicate = parse(stream)
            return lambda cm, seq: not predicate(cm, seq)
        if key == "OR":
            left, right = parse(stream), parse(stream)
            return lambda cm, seq: left(cm, seq) or right(cm, seq)
        if key in {"BEFORE", "ON", "SINCE"}:
            if not stream:
                raise ValueError("missing SEARCH date")
            wanted = _search_date(stream.pop(0))

            def matches_date(cm, seq):
                # INTERNALDATE is exposed in UTC. SEARCH ignores its time and timezone.
                actual = datetime.fromtimestamp(cm.date, timezone.utc).date()
                return actual < wanted if key == "BEFORE" else actual == wanted if key == "ON" else actual >= wanted

            return matches_date
        flags = {"SEEN": "\\Seen", "FLAGGED": "\\Flagged", "ANSWERED": "\\Answered",
                 "DELETED": "\\Deleted", "DRAFT": "\\Draft"}
        positive = key[2:] if key.startswith("UN") else key
        if positive in flags:
            expected = not key.startswith("UN")
            return lambda cm, seq: (flags[positive] in cm.flags) == expected
        if key in {"KEYWORD", "UNKEYWORD"}:
            if not stream:
                raise ValueError("missing SEARCH keyword")
            flag = str(stream.pop(0)).lower()
            return lambda cm, seq: (flag in {f.lower() for f in cm.flags}) == (key == "KEYWORD")
        if key == "UID" or re.fullmatch(r"[0-9*: ,]+", key):
            by_uid = key == "UID"
            spec = str(stream.pop(0)) if by_uid and stream else key
            selected = ranges(spec, by_uid)
            return lambda cm, seq: any(lo <= (cm.uid if by_uid else seq) <= hi for lo, hi in selected)
        raise ValueError(f"unsupported SEARCH predicate {key}")

    def conjunction(stream: list):
        predicates = []
        while stream:
            predicates.append(parse(stream))
        return lambda cm, seq: all(p(cm, seq) for p in predicates)

    if not args:
        raise ValueError("missing SEARCH criteria")
    return conjunction(args)
