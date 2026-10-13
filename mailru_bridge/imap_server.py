"""A small IMAP4rev1 server that re-exposes the cached Mail.ru mailbox to local mail clients.

It serves reads from the :class:`~mailru_bridge.cache.Cache` and maps mutations back onto the API
through the :class:`~mailru_bridge.sync.Syncer`:

* ``LIST``/``SELECT``/``STATUS`` from the cached folder tree;
* ``FETCH`` of flags, sizes, ``ENVELOPE`` (from cached metadata, no download) and, for body items,
  the real RFC 822 source (downloaded once, then cached);
* ``STORE`` → read/flag marks; ``MOVE`` → move; ``EXPUNGE`` of ``\\Deleted`` → delete (to Trash);
* ``IDLE`` backed by the background poll.

It speaks enough of the protocol for Thunderbird and FairEmail. Authentication is against the local
:class:`~mailru_bridge.config.BridgeConfig` password, never the Mail.ru one. Run it behind a VPN.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from email.utils import formatdate

from .api import ApiError, AuthExpired, MailruApi
from .cache import Cache, CachedMessage
from .config import BridgeConfig
from .imap_struct import bodystructure, envelope, envelope_from_meta
from .imap_utf7 import decode as u7decode, encode as u7encode
from .mime import Message as MimeMessage
from .outgoing import build_draft
from .search import compile_search
from .sync import Syncer

log = logging.getLogger(__name__)
MAX_LITERAL = 40 * 1024 * 1024

# Fixed Mail.ru folder ids we give IMAP special-use flags / names.
SPECIAL = {
    "0": ("INBOX", "\\Inbox"),
    "500000": ("Sent", "\\Sent"),
    "500001": ("Drafts", "\\Drafts"),
    "500002": ("Trash", "\\Trash"),
    "950": ("Spam", "\\Junk"),
    "500010": ("Archive", "\\Archive"),
}


class MailboxTree:
    """Two-way mapping between Mail.ru folder ids and IMAP mailbox path names."""

    def __init__(self, cache: Cache):
        self.cache = cache
        self.by_name: dict[str, str] = {}
        self.by_id: dict[str, str] = {}
        self.special: dict[str, str] = {}
        self.refresh()

    def refresh(self) -> None:
        self.special = {}
        rows = self.cache.folders()
        names: dict[str, str] = {}
        paths: dict[str, str] = {}
        for r in rows:
            names[r["id"]] = r["name"]
        for r in rows:
            fid = r["id"]
            if fid in SPECIAL:
                path = SPECIAL[fid][0]
                self.special[fid] = SPECIAL[fid][1]
            else:
                parts = [r["name"]]
                parent = r["parent"]
                guard = 0
                while parent and parent != "-1" and parent in names and guard < 20:
                    parts.append(names[parent])
                    prow = self.cache.folder(parent)
                    parent = prow["parent"] if prow else "-1"
                    guard += 1
                path = "/".join(reversed(parts))
            paths[fid] = path
        self.by_id = paths
        self.by_name = {v.lower(): k for k, v in paths.items()}

    def id_for(self, name: str) -> str | None:
        if name.upper() == "INBOX":
            return "0"
        return self.by_name.get(name.lower())

    def name_for(self, fid: str) -> str:
        return self.by_id.get(fid, fid)


def _wire_name(path: str) -> bytes:
    return b'"' + u7encode(path).replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def parse_sequence_set(spec: str, uids: list[int], max_val: int) -> list[tuple[int, int]]:
    """Expand an IMAP sequence/UID set like ``1:4,7,9:*`` into the matching values from ``uids``.

    ``uids`` is the ordered pool to pick from (sequence numbers map to positions; UIDs filter by
    membership). ``max_val`` is used for ``*``.
    """
    if not re.fullmatch(r"(?:[1-9][0-9]*|\*)(?::(?:[1-9][0-9]*|\*))?(?:,(?:[1-9][0-9]*|\*)(?::(?:[1-9][0-9]*|\*))?)*", spec):
        raise ValueError("invalid sequence set")
    result: list[tuple[int, int]] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            a, b = piece.split(":", 1)
            lo = max_val if a == "*" else int(a)
            hi = max_val if b == "*" else int(b)
            if lo > hi:
                lo, hi = hi, lo
            result.append((lo, hi))
        else:
            v = max_val if piece == "*" else int(piece)
            result.append((v, v))
    return result  # ranges; caller resolves against its index


class ImapConnection:
    def __init__(self, server: "ImapServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.s = server
        self.reader = reader
        self.writer = writer
        self.authed = False
        self.selected: str | None = None  # folder id
        self.readonly = False
        self.snapshot: list[int] = []  # seq (1-based) -> uid, ascending
        self.snapshot_flags: dict[int, set[str]] = {}
        self.peer = writer.get_extra_info("peername")
        self._idle = False

    # -- io --------------------------------------------------------------------------------

    async def send(self, line: bytes) -> None:
        self.writer.write(line + b"\r\n")
        await self.writer.drain()

    async def read_line(self) -> bytes | None:
        try:
            data = await self.reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        except asyncio.LimitOverrunError:
            await self.untagged("BYE command line too long")
            return None
        return data.rstrip(b"\r\n")

    async def read_command(self) -> list[tuple[str, bytes]] | None:
        """Read one command as an ordered token list.

        Each token is ``("t", <text bytes>)`` for an ordinary line segment or ``("l", <octets>)``
        for an IMAP literal (``{n}`` block). Text segments keep their spacing; a literal's trailing
        continuation line becomes the next ``"t"`` token. This preserves argument order even when a
        literal sits in the middle of a command (``LOGIN "u" {3}\\r\\nabc``).
        """
        line = await self.read_line()
        if line is None:
            return None
        tokens: list[tuple[str, bytes]] = []
        buf = line
        size = len(line)
        while True:
            m = re.search(rb"\{(\d+)(\+)?\}$", buf)
            if not m:
                tokens.append(("t", buf))
                break
            tokens.append(("t", buf[: m.start()]))
            n = int(m.group(1))
            size += n
            if size > (MAX_LITERAL if self.authed else 65536):
                await self.untagged("BYE command literal too large")
                return None
            if not m.group(2):
                await self.send(b"+ Ready for literal data")
            try:
                chunk = await self.reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            tokens.append(("l", chunk))
            nxt = await self.read_line()
            if nxt is None:
                tokens.append(("t", b""))
                break
            buf = nxt
            size += len(nxt)
        return tokens

    # -- helpers ---------------------------------------------------------------------------

    async def untagged(self, text: str | bytes) -> None:
        await self.send(b"* " + (text.encode() if isinstance(text, str) else text))

    async def tagged(self, tag: bytes, status: str, text: str) -> None:
        await self.send(tag + b" " + status.encode() + b" " + text.encode())

    def _resolve(self, ranges, by_uid: bool) -> list[tuple[int, int]]:
        """Given parsed ranges and the current snapshot, yield (seq, uid) pairs in order."""
        out = []
        n = len(self.snapshot)
        if by_uid:
            for lo, hi in ranges:
                for seq, uid in enumerate(self.snapshot, 1):
                    if lo <= uid <= hi:
                        out.append((seq, uid))
        else:
            for lo, hi in ranges:
                for seq in range(max(1, lo), min(n, hi) + 1):
                    out.append((seq, self.snapshot[seq - 1]))
        # de-dup preserving order
        seen = set()
        uniq = []
        for item in out:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        return uniq

    def _ranges(self, spec: str, by_uid: bool) -> list[tuple[int, int]]:
        maximum = max(self.snapshot, default=0) if by_uid else len(self.snapshot)
        return parse_sequence_set(spec, self.snapshot, maximum)

    # -- command loop ----------------------------------------------------------------------

    async def serve(self) -> None:
        caps = self.s.capabilities()
        await self.untagged(f"OK [CAPABILITY {caps}] mailru-bridge ready")
        while True:
            tokens = await self.read_command()
            if tokens is None:
                break
            try:
                if not await self.dispatch(tokens):
                    break
            except AuthExpired as exc:
                await self._fail_all("session expired; re-run `mailru-bridge login`", exc)
            except ApiError as exc:
                log.warning("api error: %s", exc)
                await self.last_tagged("NO", "upstream request failed")
            except (ValueError, IndexError):
                await self.last_tagged("BAD", "invalid command arguments")
            except Exception as exc:  # noqa: BLE001 — never kill the connection on one bad command
                log.exception("command failed")
                await self.last_tagged("NO", "internal error")
        try:
            self.writer.close()
        except Exception:
            pass

    async def _fail_all(self, msg: str, exc: Exception) -> None:
        await self.last_tagged("NO", msg)

    async def last_tagged(self, status: str, text: str) -> None:
        tag = getattr(self, "_tag", b"*")
        await self.tagged(tag, status, text)

    async def dispatch(self, tokens: list[tuple[str, bytes]]) -> bool:
        # tokens[0] is ("t", "<tag> <command> <args...>"); later tokens are literals + line tails.
        head = tokens[0][1].split(b" ", 2)
        if len(head) < 2:
            await self.send(b"* BAD empty command")
            return True
        tag = head[0]
        self._tag = tag
        command = head[1].upper().decode("ascii", "replace")
        rest = head[2] if len(head) > 2 else b""
        rest_tokens: list[tuple[str, bytes]] = [("t", rest)] + tokens[1:]

        if not self.authed and command not in {"CAPABILITY", "NOOP", "LOGOUT", "ID", "LOGIN", "AUTHENTICATE"}:
            await self.tagged(tag, "NO", "LOGIN first")
            return True
        if self.authed and command in {"LOGIN", "AUTHENTICATE"}:
            await self.tagged(tag, "BAD", "already authenticated")
            return True
        operation = rest.split(b" ", 1)[0].upper().decode("ascii", "replace") if command == "UID" else command
        if operation in {"FETCH", "STORE", "SEARCH", "MOVE", "COPY", "EXPUNGE", "CHECK", "CLOSE", "UNSELECT", "IDLE"} and not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        if self.readonly and operation in {"STORE", "MOVE", "EXPUNGE"}:
            await self.tagged(tag, "NO", "mailbox is read-only")
            return True

        import time as _t

        t0 = _t.monotonic()
        try:
            if command == "UID":
                return await self.cmd_uid(tag, rest, rest_tokens)
            handler = getattr(self, f"cmd_{command.lower().replace('.', '_')}", None)
            if handler is None:
                await self.tagged(tag, "BAD", f"unknown command {command}")
                return True
            return await handler(tag, rest, rest_tokens)
        finally:
            dt = _t.monotonic() - t0
            if dt > 1.0:
                log.info("IMAP[%s] %s -> %.1fs (snap=%d)",
                         self.peer[0] if self.peer else "?", command, dt, len(self.snapshot))

    # -- basic -----------------------------------------------------------------------------

    async def cmd_capability(self, tag, rest, tokens) -> bool:
        await self.untagged(f"CAPABILITY {self.s.capabilities()}")
        await self.tagged(tag, "OK", "CAPABILITY completed")
        return True

    async def cmd_noop(self, tag, rest, tokens) -> bool:
        if self.selected:
            await self.poll_selected()
        await self.tagged(tag, "OK", "NOOP completed")
        return True

    async def cmd_logout(self, tag, rest, tokens) -> bool:
        await self.untagged("BYE mailru-bridge signing off")
        await self.tagged(tag, "OK", "LOGOUT completed")
        return False

    async def cmd_id(self, tag, rest, tokens) -> bool:
        await self.untagged('ID ("name" "mailru-bridge")')
        await self.tagged(tag, "OK", "ID completed")
        return True

    async def cmd_namespace(self, tag, rest, tokens) -> bool:
        await self.untagged('NAMESPACE (("" "/")) NIL NIL')
        await self.tagged(tag, "OK", "NAMESPACE completed")
        return True

    async def cmd_check(self, tag, rest, tokens) -> bool:
        await self.poll_selected()
        await self.tagged(tag, "OK", "CHECK completed")
        return True

    async def cmd_login(self, tag, rest, tokens) -> bool:
        args = _split_astrings(tokens)
        if len(args) < 2:
            await self.tagged(tag, "BAD", "LOGIN needs user and password")
            return True
        user = _astr(args[0])
        pw = _astr(args[1])
        if await asyncio.to_thread(self.s.config.check_login, user, pw):
            self.authed = True
            await self.tagged(tag, "OK", "LOGIN completed")
        else:
            await asyncio.sleep(1.0)
            await self.tagged(tag, "NO", "invalid bridge credentials")
        return True

    async def cmd_authenticate(self, tag, rest, tokens) -> bool:
        # Minimal AUTH=PLAIN support (some clients prefer it).
        import base64

        mech = rest.split(b" ", 1)[0].upper()
        if mech != b"PLAIN":
            await self.tagged(tag, "NO", "only AUTH=PLAIN supported")
            return True
        await self.send(b"+ ")
        line = await self.read_line()
        try:
            decoded = base64.b64decode(line, validate=True).split(b"\x00")
            if len(decoded) != 3 or (decoded[0] and decoded[0] != decoded[1]):
                raise ValueError("invalid authorization identity")
            user, pw = decoded[1].decode(), decoded[2].decode()
        except Exception:
            await self.tagged(tag, "NO", "invalid SASL response")
            return True
        if await asyncio.to_thread(self.s.config.check_login, user, pw):
            self.authed = True
            await self.tagged(tag, "OK", "AUTHENTICATE completed")
        else:
            await asyncio.sleep(1.0)
            await self.tagged(tag, "NO", "invalid bridge credentials")
        return True

    def _require_auth(self) -> bool:
        return self.authed

    # -- mailboxes -------------------------------------------------------------------------

    async def cmd_list(self, tag, rest, tokens) -> bool:
        if not self.authed:
            await self.tagged(tag, "NO", "LOGIN first")
            return True
        await self.s.refresh_tree()
        args = _split_astrings(tokens)
        reference = _mailbox_name(args[0]) if args else ""
        pattern = _mailbox_name(args[1]) if len(args) > 1 else "*"
        if not pattern:
            await self.untagged(b'LIST (\\Noselect) "/" ""')
            await self.tagged(tag, "OK", "LIST completed")
            return True
        pattern = reference + pattern
        regex = _wildcard_to_regex(pattern)
        for fid, path in sorted(self.s.tree.by_id.items(), key=lambda kv: kv[1]):
            if regex.match(path) or (path.upper() == "INBOX" and regex.match("INBOX")):
                flags = ["\\HasNoChildren"]
                if fid in self.s.tree.special:
                    flags.append(self.s.tree.special[fid])
                if self.s.has_children(fid):
                    flags = [f for f in flags if f != "\\HasNoChildren"] + ["\\HasChildren"]
                await self.untagged(b'LIST (' + " ".join(flags).encode() + b') "/" ' + _wire_name(path))
        await self.tagged(tag, "OK", "LIST completed")
        return True

    async def cmd_lsub(self, tag, rest, tokens) -> bool:
        return await self.cmd_list(tag, rest, tokens)

    async def cmd_subscribe(self, tag, rest, tokens) -> bool:
        await self.tagged(tag, "OK", "SUBSCRIBE completed")
        return True

    async def cmd_unsubscribe(self, tag, rest, tokens) -> bool:
        await self.tagged(tag, "OK", "UNSUBSCRIBE completed")
        return True

    async def cmd_status(self, tag, rest, tokens) -> bool:
        args = _split_astrings(tokens)
        name = _mailbox_name(args[0])
        fid = self.s.tree.id_for(name)
        if fid is None:
            await self.tagged(tag, "NO", "no such mailbox")
            return True
        await self.s.sync_folder_quietly(fid)
        exists, unseen = self.s.cache.counts(fid)
        folder = self.s.cache.folder(fid)
        items = rest.decode("latin-1").upper()
        out = []
        if "MESSAGES" in items:
            out.append(f"MESSAGES {exists}")
        if "UNSEEN" in items:
            out.append(f"UNSEEN {unseen}")
        if "UIDNEXT" in items:
            out.append(f"UIDNEXT {folder['uidnext']}")
        if "UIDVALIDITY" in items:
            out.append(f"UIDVALIDITY {folder['uidvalidity']}")
        if "RECENT" in items:
            out.append("RECENT 0")
        await self.untagged(b"STATUS " + _wire_name(self.s.tree.name_for(fid)) + b" (" + " ".join(out).encode() + b")")
        await self.tagged(tag, "OK", "STATUS completed")
        return True

    async def _select(self, tag, rest, tokens, readonly: bool) -> bool:
        if not self.authed:
            await self.tagged(tag, "NO", "LOGIN first")
            return True
        args = _split_astrings(tokens)
        name = _mailbox_name(args[0])
        fid = self.s.tree.id_for(name)
        if fid is None:
            await self.tagged(tag, "NO", "no such mailbox")
            return True
        await self.s.ensure_initial(fid)
        self.selected = fid
        self.readonly = readonly
        self._rebuild_snapshot()
        folder = self.s.cache.folder(fid)
        exists, unseen = self.s.cache.counts(fid)
        await self.untagged(f"{exists} EXISTS")
        await self.untagged("0 RECENT")
        await self.untagged(r'FLAGS (\Seen \Flagged \Answered \Deleted \Draft $Forwarded)')
        await self.untagged('OK [PERMANENTFLAGS ()] read-only' if readonly else
                            r'OK [PERMANENTFLAGS (\Seen \Flagged \Answered \Deleted $Forwarded)] limited')
        await self.untagged(f"OK [UIDVALIDITY {folder['uidvalidity']}] validity")
        await self.untagged(f"OK [UIDNEXT {folder['uidnext']}] next")
        if unseen:
            first_unseen = self._first_unseen()
            if first_unseen:
                await self.untagged(f"OK [UNSEEN {first_unseen}] first unseen")
        await self.tagged(tag, "OK", f"[{'READ-ONLY' if readonly else 'READ-WRITE'}] SELECT completed")
        return True

    async def cmd_select(self, tag, rest, tokens) -> bool:
        return await self._select(tag, rest, tokens, readonly=False)

    async def cmd_examine(self, tag, rest, tokens) -> bool:
        return await self._select(tag, rest, tokens, readonly=True)

    async def cmd_close(self, tag, rest, tokens) -> bool:
        if self.selected and not self.readonly:
            await self._expunge(silent=True)
        self.selected = None
        self.snapshot = []
        await self.tagged(tag, "OK", "CLOSE completed")
        return True

    async def cmd_unselect(self, tag, rest, tokens) -> bool:
        self.selected = None
        self.snapshot = []
        await self.tagged(tag, "OK", "UNSELECT completed")
        return True

    async def cmd_create(self, tag, rest, tokens) -> bool:
        args = _split_astrings(tokens)
        path = u7decode(_astr(args[0]).encode("latin-1")).rstrip("/")
        parent_id = "-1"
        name = path
        if "/" in path:
            parent_path, name = path.rsplit("/", 1)
            parent_id = self.s.tree.id_for(parent_path) or "-1"
        try:
            await self.s.api.create_folder(name, parent_id)
        except ApiError as exc:
            await self.tagged(tag, "NO", f"CREATE failed: {exc}")
            return True
        await self.s.sync_and_refresh()
        await self.tagged(tag, "OK", "CREATE completed")
        return True

    async def cmd_delete(self, tag, rest, tokens) -> bool:
        args = _split_astrings(tokens)
        fid = self.s.tree.id_for(_mailbox_name(args[0]))
        if fid is None:
            await self.tagged(tag, "NO", "no such mailbox")
            return True
        try:
            await self.s.api.delete_folder(fid)
        except ApiError as exc:
            await self.tagged(tag, "NO", f"DELETE failed: {exc}")
            return True
        await self.s.sync_and_refresh()
        await self.tagged(tag, "OK", "DELETE completed")
        return True

    async def cmd_rename(self, tag, rest, tokens) -> bool:
        args = _split_astrings(tokens)
        fid = self.s.tree.id_for(_mailbox_name(args[0]))
        if fid is None:
            await self.tagged(tag, "NO", "no such mailbox")
            return True
        newpath = u7decode(_astr(args[1]).encode("latin-1"))
        name = newpath.rsplit("/", 1)[-1]
        try:
            await self.s.api.rename_folder(fid, name)
        except ApiError as exc:
            await self.tagged(tag, "NO", f"RENAME failed: {exc}")
            return True
        await self.s.sync_and_refresh()
        await self.tagged(tag, "OK", "RENAME completed")
        return True

    # -- snapshot --------------------------------------------------------------------------

    def _rebuild_snapshot(self) -> None:
        msgs = self.s.cache.messages(self.selected)
        self.snapshot = [m.uid for m in msgs]
        self.snapshot_flags = {m.uid: set(m.flags) for m in msgs}

    def _first_unseen(self) -> int | None:
        for seq, uid in enumerate(self.snapshot, 1):
            cm = self.s.cache.message_by_uid(self.selected, uid)
            if cm and not cm.seen:
                return seq
        return None

    async def poll_selected(self) -> None:
        """Re-sync the selected folder and emit EXISTS/EXPUNGE for any change."""
        if not self.selected:
            return
        await self.s.incremental(self.selected)
        # Another connection or the background poll may have already applied the sync.
        # Compare this connection's view with the cache rather than relying on that sync's delta.
        messages = self.s.cache.messages(self.selected)
        live = {m.uid for m in messages}
        expunged = set(self.snapshot) - live
        # EXPUNGE first (highest seq first), against the old snapshot
        if expunged:
            exp_set = set(expunged)
            for seq in range(len(self.snapshot), 0, -1):
                if self.snapshot[seq - 1] in exp_set:
                    await self.untagged(f"{seq} EXPUNGE")
                    del self.snapshot[seq - 1]
        if live - set(self.snapshot):
            # append newly-arrived uids (ascending) to the snapshot
            current = set(self.snapshot)
            for cm in messages:
                if cm.uid not in current:
                    self.snapshot.append(cm.uid)
            await self.untagged(f"{len(self.snapshot)} EXISTS")
        by_uid = {m.uid: m for m in messages}
        for seq, uid in enumerate(self.snapshot, 1):
            cm = by_uid[uid]
            if uid in self.snapshot_flags and cm.flags != self.snapshot_flags[uid]:
                await self.untagged(b"%d FETCH (UID %d FLAGS (" % (seq, uid)
                                    + " ".join(sorted(cm.flags)).encode() + b"))")
        self.snapshot_flags = {m.uid: set(m.flags) for m in messages}

    # -- FETCH -----------------------------------------------------------------------------

    async def cmd_fetch(self, tag, rest, tokens, by_uid=False) -> bool:
        if not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        m = re.match(rb"\s*(\S+)\s+(.*)$", rest, re.DOTALL)
        if not m:
            await self.tagged(tag, "BAD", "FETCH syntax")
            return True
        seqset = m.group(1).decode("latin-1")
        items_spec = m.group(2).decode("latin-1").strip()
        ranges = self._ranges(seqset, by_uid)
        targets = self._resolve(ranges, by_uid)
        items = _parse_fetch_items(items_spec)
        # If any item needs the raw message, download all of them concurrently (bounded) before
        # formatting — otherwise a multi-message FETCH would fetch bodies one-at-a-time and crawl.
        if targets and any(_needs_raw(it) for it in items):
            await self._prefetch_raw([uid for _, uid in targets])
        for seq, uid in targets:
            resp = await self._fetch_one(seq, uid, items, by_uid)
            if resp is not None:
                await self.untagged(resp)
        await self.tagged(tag, "OK", ("UID " if by_uid else "") + "FETCH completed")
        return True

    async def _prefetch_raw(self, uids: list[int], concurrency: int = 10) -> None:
        """Warm the raw-source cache for many messages at once, bounded by a semaphore.

        The shared rate limiter still spaces request starts; this just lets the ~1.5s downloads
        overlap instead of running strictly in series.
        """
        need = [u for u in uids if self.s.cache.get_raw(self.selected, u) is None]
        if not need:
            return
        log.info("prefetch: %d/%d bodies need download (folder %s)", len(need), len(uids), self.selected)
        sem = asyncio.Semaphore(concurrency)

        async def one(u: int) -> None:
            async with sem:
                try:
                    await self.s.syncer.ensure_raw(self.selected, u)
                except Exception:  # noqa: BLE001 — a single bad message must not fail the batch
                    log.debug("prefetch raw failed for uid %s", u, exc_info=True)

        await asyncio.gather(*(one(u) for u in need))

    async def _fetch_one(self, seq: int, uid: int, items: list[str], by_uid: bool) -> bytes | None:
        cm = self.s.cache.message_by_uid(self.selected, uid)
        if cm is None:
            return None
        pieces: list[bytes] = []
        need_raw = any(_needs_raw(it) for it in items)
        mime: MimeMessage | None = None
        mark_seen = False
        if need_raw:
            raw = await self.s.syncer.ensure_raw(self.selected, uid)
            mime = MimeMessage(raw)
            cm = self.s.cache.message_by_uid(self.selected, uid)  # size may have been corrected

        if by_uid and not any(it.upper() == "UID" for it in items):
            items = items + ["UID"]

        for it in items:
            up = it.upper()
            if up == "UID":
                pieces.append(b"UID %d" % uid)
            elif up == "FLAGS":
                pieces.append(b"FLAGS (" + " ".join(sorted(cm.flags)).encode() + b")")
            elif up == "INTERNALDATE":
                pieces.append(b'INTERNALDATE "' + _internaldate(cm.date).encode() + b'"')
            elif up == "RFC822.SIZE":
                pieces.append(b"RFC822.SIZE %d" % cm.size)
            elif up == "ENVELOPE":
                env = envelope(mime) if mime else envelope_from_meta(cm.meta, cm.date)
                pieces.append(b"ENVELOPE " + env)
            elif up == "BODYSTRUCTURE":
                pieces.append(b"BODYSTRUCTURE " + bodystructure(mime, extensible=True))
            elif up == "BODY" and mime:
                pieces.append(b"BODY " + bodystructure(mime, extensible=False))
            elif up in ("RFC822", "RFC822.HEADER", "RFC822.TEXT"):
                section = {"RFC822": "", "RFC822.HEADER": "HEADER", "RFC822.TEXT": "TEXT"}[up]
                data = mime.section_bytes(section) or b""
                if up != "RFC822.HEADER":
                    mark_seen = True
                pieces.append(up.encode() + b" " + _literal(data))
            elif up.startswith("BODY"):
                piece, seen = self._fetch_body_item(it, mime)
                if piece is not None:
                    pieces.append(piece)
                    mark_seen = mark_seen or seen
        if mark_seen and not self.readonly and not cm.seen:
            await self.s.set_seen(self.selected, uid, True)
            cm = self.s.cache.message_by_uid(self.selected, uid)
            # reflect the new flag in this very response
            pieces = [b"FLAGS (" + " ".join(sorted(cm.flags)).encode() + b")" if p.startswith(b"FLAGS") else p
                      for p in pieces]
            if not any(p.startswith(b"FLAGS") for p in pieces):
                pieces.append(b"FLAGS (" + " ".join(sorted(cm.flags)).encode() + b")")
        return b"%d FETCH (" % seq + b" ".join(pieces) + b")"

    def _fetch_body_item(self, item: str, mime: MimeMessage | None) -> tuple[bytes | None, bool]:
        m = re.match(r"BODY(\.PEEK)?\[([^\]]*)\](?:<(\d+)(?:\.(\d+))?>)?$", item, re.IGNORECASE)
        if not m or mime is None:
            return None, False
        peek = bool(m.group(1))
        section = m.group(2)
        start = m.group(3)
        length = m.group(4)
        data = mime.section_bytes(section)
        if data is None:
            data = b""
        label = f"BODY[{section}]"
        if start is not None:
            s = int(start)
            data = data[s:] if length is None else data[s : s + int(length)]
            label += f"<{start}>"
        return label.encode() + b" " + _literal(data), (not peek)

    # -- STORE -----------------------------------------------------------------------------

    async def cmd_store(self, tag, rest, tokens, by_uid=False) -> bool:
        if not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        m = re.match(rb"\s*(\S+)\s+([+-]?FLAGS(?:\.SILENT)?)\s+\(?([^)]*)\)?\s*$", rest, re.IGNORECASE)
        if not m:
            await self.tagged(tag, "BAD", "STORE syntax")
            return True
        seqset = m.group(1).decode("latin-1")
        op = m.group(2).decode("latin-1").upper()
        canonical = {f.lower(): f for f in ("\\Seen", "\\Flagged", "\\Answered", "\\Deleted", "$Forwarded")}
        requested = m.group(3).decode("latin-1").split()
        if any(f.lower() not in canonical for f in requested):
            await self.tagged(tag, "NO", "unsupported flag")
            return True
        flags = {canonical[f.lower()] for f in requested}
        silent = "SILENT" in op
        mode = "+" if op.startswith("+") else "-" if op.startswith("-") else "="
        ranges = self._ranges(seqset, by_uid)
        for seq, uid in self._resolve(ranges, by_uid):
            newflags = await self.s.apply_flags(self.selected, uid, mode, flags)
            if not silent:
                extra = (b" UID %d" % uid) if by_uid else b""
                await self.untagged(b"%d FETCH (FLAGS (" % seq + " ".join(sorted(newflags)).encode() + b")" + extra + b")")
        await self.tagged(tag, "OK", ("UID " if by_uid else "") + "STORE completed")
        return True

    # -- COPY / MOVE -----------------------------------------------------------------------

    async def cmd_copy(self, tag, rest, tokens, by_uid=False) -> bool:
        await self.tagged(tag, "NO", "COPY not supported (use MOVE); Mail.ru has no copy")
        return True

    async def cmd_move(self, tag, rest, tokens, by_uid=False) -> bool:
        if not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        args = _split_astrings(tokens)  # [seqset, mailbox]
        if len(args) < 2:
            await self.tagged(tag, "BAD", "MOVE syntax")
            return True
        dest = self.s.tree.id_for(_mailbox_name(args[1]))
        if dest is None:
            await self.tagged(tag, "NO", "no such destination mailbox")
            return True
        ranges = self._ranges(_astr(args[0]), by_uid)
        targets = self._resolve(ranges, by_uid)
        ids = [self.s.cache.message_by_uid(self.selected, uid).mailru_id for _, uid in targets
               if self.s.cache.message_by_uid(self.selected, uid)]
        if ids:
            uids = [uid for _, uid in targets]
            async with self.s._lock:
                await self.s.api.move(ids, dest)
                self.s.cache.forget(self.selected, uids)
            exp_set = set(uids)
            for seq in range(len(self.snapshot), 0, -1):
                if self.snapshot[seq - 1] in exp_set:
                    await self.untagged(f"{seq} EXPUNGE")
                    del self.snapshot[seq - 1]
        await self.tagged(tag, "OK", ("UID " if by_uid else "") + "MOVE completed")
        return True

    # -- EXPUNGE ---------------------------------------------------------------------------

    async def cmd_expunge(self, tag, rest, tokens) -> bool:
        if not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        await self._expunge(silent=False)
        await self.tagged(tag, "OK", "EXPUNGE completed")
        return True

    async def _expunge(self, silent: bool, uids: set[int] | None = None) -> None:
        async with self.s._lock:
            await self._expunge_locked(silent, uids)

    async def _expunge_locked(self, silent: bool, uids: set[int] | None) -> None:
        to_delete = [(seq, uid) for seq, uid in enumerate(self.snapshot, 1)
                     if self._has_deleted(uid) and (uids is None or uid in uids)]
        if not to_delete:
            return
        ids = [self.s.cache.message_by_uid(self.selected, uid).mailru_id for _, uid in to_delete
               if self.s.cache.message_by_uid(self.selected, uid)]
        if ids:
            # Mail.ru only permanently removes from Trash/Spam; elsewhere "delete" = move to Trash.
            if self.selected in ("500002", "950"):
                await self.s.api.remove(ids)
            else:
                await self.s.api.move(ids, "500002")
        for seq, uid in sorted(to_delete, reverse=True):
            self.s.cache.forget(self.selected, [uid])
            idx = self.snapshot.index(uid)
            del self.snapshot[idx]
            if not silent:
                await self.untagged(f"{idx + 1} EXPUNGE")

    def _has_deleted(self, uid: int) -> bool:
        cm = self.s.cache.message_by_uid(self.selected, uid)
        return bool(cm and "\\Deleted" in cm.flags)

    # -- SEARCH ----------------------------------------------------------------------------

    async def cmd_search(self, tag, rest, tokens, by_uid=False) -> bool:
        if not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        try:
            predicate = compile_search(_split_astrings(tokens), _split_astrings, self._ranges)
        except ValueError as exc:
            await self.tagged(tag, "BAD", str(exc))
            return True
        results = []
        for seq, uid in enumerate(self.snapshot, 1):
            cm = self.s.cache.message_by_uid(self.selected, uid)
            if cm is not None and predicate(cm, seq):
                results.append(seq)
        if by_uid:
            values = [self.snapshot[seq - 1] for seq in results]
        else:
            values = results
        await self.untagged("SEARCH " + " ".join(str(v) for v in values))
        await self.tagged(tag, "OK", ("UID " if by_uid else "") + "SEARCH completed")
        return True

    # -- APPEND ----------------------------------------------------------------------------

    async def cmd_append(self, tag, rest, tokens) -> bool:
        # APPEND <mailbox> [(flags)] ["date"] {literal}
        args = _split_astrings(tokens)
        if not args:
            await self.tagged(tag, "BAD", "APPEND syntax")
            return True
        name = _mailbox_name(args[0])
        fid = self.s.tree.id_for(name)
        literal = next((data for kind, data in tokens if kind == "l"), None)
        if fid == "500000":
            await self.tagged(tag, "NO", "Sent is server-managed; disable client Sent APPEND")
            return True
        if fid == "500001" and literal:  # Drafts: create a draft from the message
            try:
                await self.s.save_draft(literal)
                await self.s.sync_folder_quietly("500001")
                await self.tagged(tag, "OK", "APPEND (draft) completed")
            except ApiError as exc:
                await self.tagged(tag, "NO", f"APPEND failed: {exc}")
            return True
        await self.tagged(tag, "NO", "APPEND to this mailbox is not supported by the Mail.ru API")
        return True

    # -- IDLE ------------------------------------------------------------------------------

    async def cmd_idle(self, tag, rest, tokens) -> bool:
        if not self.selected:
            await self.tagged(tag, "NO", "no mailbox selected")
            return True
        await self.send(b"+ idling")
        self._idle = True
        done = asyncio.create_task(self._await_done())
        try:
            while not done.done():
                try:
                    await asyncio.wait_for(asyncio.shield(done), timeout=self.s.config.poll_seconds)
                except asyncio.TimeoutError:
                    await self.poll_selected()
        finally:
            self._idle = False
            done.cancel()
            await asyncio.gather(done, return_exceptions=True)
        await self.tagged(tag, "OK", "IDLE terminated")
        return True

    async def _await_done(self) -> None:
        while True:
            line = await self.read_line()
            if line is None or line.strip().upper() == b"DONE":
                return

    # -- UID dispatcher --------------------------------------------------------------------

    async def cmd_uid(self, tag, rest, tokens) -> bool:
        sub = rest.split(b" ", 1)
        if len(sub) < 2:
            await self.tagged(tag, "BAD", "UID needs a subcommand")
            return True
        name = sub[0].upper().decode("ascii", "replace")
        remainder = sub[1]
        tokens = [("t", remainder)] + tokens[1:]
        self._tag = tag
        if name == "FETCH":
            return await self.cmd_fetch(tag, remainder, tokens, by_uid=True)
        if name == "STORE":
            return await self.cmd_store(tag, remainder, tokens, by_uid=True)
        if name == "SEARCH":
            return await self.cmd_search(tag, remainder, tokens, by_uid=True)
        if name == "COPY":
            return await self.cmd_copy(tag, remainder, tokens, by_uid=True)
        if name == "MOVE":
            return await self.cmd_move(tag, remainder, tokens, by_uid=True)
        if name == "EXPUNGE":
            ranges = self._ranges(remainder.decode("ascii").strip(), by_uid=True)
            await self._expunge(silent=False, uids={uid for _, uid in self._resolve(ranges, True)})
            await self.tagged(tag, "OK", "UID EXPUNGE completed")
            return True
        await self.tagged(tag, "BAD", f"unknown UID subcommand {name}")
        return True


# -- parsing helpers --------------------------------------------------------------------------


def _astr(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _mailbox_name(value) -> str:
    return u7decode(_astr(value))


def _split_astrings(tokens: list[tuple[str, bytes]]) -> list:
    """Split a token list into IMAP astrings, with literals passed through as ``bytes``.

    ``tokens`` is the ``[("t", text), ("l", octets), ...]`` form from :meth:`read_command` (after
    the tag/command have been stripped). Quoted strings and atoms come back as ``str``; literal
    blocks come back as ``bytes``; parenthesised groups come back verbatim as ``bytes``.
    """
    out: list = []
    for kind, data in tokens:
        if kind == "l":
            out.append(data)
            continue
        rest = data
        i = 0
        n = len(rest)
        while i < n:
            c = rest[i : i + 1]
            if c in (b" ", b"\t"):
                i += 1
                continue
            if c == b'"':
                j = i + 1
                buf = bytearray()
                while j < n and rest[j : j + 1] != b'"':
                    if rest[j : j + 1] == b"\\":
                        j += 1
                    buf += rest[j : j + 1]
                    j += 1
                out.append(bytes(buf).decode("utf-8", "replace"))
                if j >= n:
                    raise ValueError("unterminated quoted string")
                i = j + 1
            elif c == b"(":
                depth = 1
                j = i + 1
                quoted = False
                while j < n and depth:
                    if rest[j : j + 1] == b"\\" and quoted:
                        j += 2
                        continue
                    if rest[j : j + 1] == b'"':
                        quoted = not quoted
                    elif rest[j : j + 1] == b"(" and not quoted:
                        depth += 1
                    elif rest[j : j + 1] == b")" and not quoted:
                        depth -= 1
                    j += 1
                if depth:
                    raise ValueError("unterminated parenthesised group")
                out.append(rest[i:j])
                i = j
            else:
                j = i
                while j < n and rest[j : j + 1] not in (b" ", b"\t", b"(", b")"):
                    j += 1
                if j == i:
                    raise ValueError("unexpected closing parenthesis")
                out.append(rest[i:j].decode("utf-8", "replace"))
                i = j
    return out


def _parse_fetch_items(spec: str) -> list[str]:
    spec = spec.strip()
    macros = {
        "ALL": ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"],
        "FAST": ["FLAGS", "INTERNALDATE", "RFC822.SIZE"],
        "FULL": ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE", "BODY"],
    }
    if spec.upper() in macros:
        return macros[spec.upper()]
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1]
    items: list[str] = []
    i = 0
    n = len(spec)
    while i < n:
        if spec[i] == " ":
            i += 1
            continue
        j = i
        depth = 0
        while j < n and (spec[j] != " " or depth):
            if spec[j] == "[":
                depth += 1
            elif spec[j] == "]":
                depth -= 1
            j += 1
        # include a trailing <start.len> partial
        if j < n and spec[j : j + 1] == "<":
            k = spec.find(">", j)
            if k != -1:
                j = k + 1
        items.append(spec[i:j])
        i = j
    return items


def _needs_raw(item: str) -> bool:
    up = item.upper()
    if up in ("ENVELOPE",):
        return False  # answered from metadata
    if up in ("FLAGS", "UID", "INTERNALDATE", "RFC822.SIZE"):
        return False
    if up in ("BODYSTRUCTURE", "BODY", "RFC822", "RFC822.HEADER", "RFC822.TEXT"):
        return True
    return up.startswith("BODY")


def _literal(data: bytes) -> bytes:
    return b"{%d}\r\n" % len(data) + data


def _internaldate(epoch: int) -> str:
    import time

    if not epoch:
        epoch = int(time.time())
    return time.strftime("%d-%b-%Y %H:%M:%S +0000", time.gmtime(epoch))


def _wildcard_to_regex(pattern: str) -> re.Pattern:
    out = ["^"]
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "%":
            out.append("[^/]*")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return re.compile("".join(out), re.IGNORECASE)


# -- server -----------------------------------------------------------------------------------


class ImapServer:
    def __init__(self, account, api: MailruApi, cache: Cache, syncer: Syncer, config: BridgeConfig):
        self.account = account
        self.api = api
        self.cache = cache
        self.syncer = syncer
        self.config = config
        self.tree = MailboxTree(cache)
        self._initial: set[str] = set()
        self._lock = asyncio.Lock()

    def capabilities(self) -> str:
        caps = ["IMAP4rev1", "AUTH=PLAIN", "IDLE", "MOVE", "LITERAL+", "NAMESPACE", "ID", "UNSELECT"]
        return " ".join(caps)

    def has_children(self, fid: str) -> bool:
        return any(r["parent"] == fid for r in self.cache.folders())

    async def refresh_tree(self) -> None:
        await self.sync_folders()
        self.tree.refresh()

    async def sync_and_refresh(self) -> None:
        await self.refresh_tree()

    async def sync_folders(self) -> None:
        async with self._lock:
            await self.syncer.sync_folders()
        self.tree.refresh()

    def _deep_synced(self, fid: str) -> bool:
        # Persisted so a restart doesn't re-pull history (the cache survives). A shallow STATUS
        # sync must NOT set this, or SELECT would never load the full depth.
        return self.cache.get_meta(f"deep:{fid}") == "1"

    async def ensure_initial(self, fid: str) -> None:
        """On SELECT: do the full initial sync once per folder (to initial_depth), else incremental."""
        async with self._lock:
            if not self._deep_synced(fid):
                await self.syncer.initial_sync(fid, depth=self.config.initial_depth)
                self.cache.set_meta(f"deep:{fid}", "1")
            else:
                await self.syncer.incremental_sync(fid, window=self.config.sync_window)
        self._initial.add(fid)

    async def sync_folder_quietly(self, fid: str) -> None:
        """For STATUS: a light touch only. Never claims a deep sync, so SELECT still loads depth."""
        async with self._lock:
            if self._deep_synced(fid):
                await self.syncer.incremental_sync(fid, window=self.config.sync_window)
            else:
                folder = self.cache.folder(fid)
                if folder is None or folder["last_sync"] == 0:
                    await self.syncer.initial_sync(fid, depth=min(self.config.initial_depth, 300))

    async def incremental(self, fid: str) -> tuple[int, list[int]]:
        async with self._lock:
            return await self.syncer.incremental_sync(fid, window=self.config.sync_window)

    async def set_seen(self, fid: str, uid: int, seen: bool) -> None:
        async with self._lock:
            cm = self.cache.message_by_uid(fid, uid)
            if cm is None:
                return
            if seen:
                await self.api.mark(fid, read=[cm.mailru_id])
                cm.flags.add("\\Seen")
            else:
                await self.api.mark(fid, unread=[cm.mailru_id])
                cm.flags.discard("\\Seen")
            self.cache.update_flags(fid, uid, cm.flags)

    async def apply_flags(self, fid: str, uid: int, mode: str, flags: set[str]) -> set[str]:
        async with self._lock:
            cm = self.cache.message_by_uid(fid, uid)
            if cm is None:
                return set()
            current = set(cm.flags)
            if mode == "=":
                target = set(flags)
            elif mode == "+":
                target = current | flags
            else:
                target = current - flags
            # push the deltas the API understands
            mid = cm.mailru_id
            was_seen, now_seen = "\\Seen" in current, "\\Seen" in target
            was_flag, now_flag = "\\Flagged" in current, "\\Flagged" in target
            if now_seen and not was_seen:
                await self.api.mark(fid, read=[mid])
            elif was_seen and not now_seen:
                await self.api.mark(fid, unread=[mid])
            if now_flag and not was_flag:
                await self.api.mark(fid, flag=[mid])
            elif was_flag and not now_flag:
                await self.api.mark(fid, unflag=[mid])
            self.cache.update_flags(fid, uid, target)
            return target

    async def save_draft(self, raw: bytes) -> None:
        await self.api.save_draft(build_draft(raw, self.account.email))

    async def serve(self) -> None:
        await self.sync_folders()
        ssl_ctx = None
        if self.config.tls:
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(self.config.tls_cert, self.config.tls_key)
        server = await asyncio.start_server(
            self._handle, self.config.bind_host, self.config.imap_port, ssl=ssl_ctx
        )
        addr = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("IMAP listening on %s (TLS=%s)", addr, bool(ssl_ctx))
        async with server:
            await server.serve_forever()

    async def _handle(self, reader, writer) -> None:
        conn = ImapConnection(self, reader, writer)
        try:
            await conn.serve()
        except Exception:
            log.exception("connection error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
