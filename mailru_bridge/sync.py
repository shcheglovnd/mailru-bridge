r"""Keep the local :class:`~mailru_bridge.cache.Cache` current from the Mail.ru API.

Reading clients are served entirely from the cache; this module is the only thing that talks to the
API for mail content. It does three jobs:

* **folders** — mirror the folder list into the cache.
* **initial sync** — page a folder newest-first (bounded by ``depth``) and assign each message a
  stable IMAP UID, inserting oldest-first so UIDs rise with date within the first batch.
* **incremental sync** — pull the newest page, add anything unseen, refresh flags, and notice server
  side deletions by diffing the newest window against the cache.

Flag mapping (Mail.ru → IMAP): ``unread=false`` → ``\Seen``; ``flagged`` → ``\Flagged``;
``reply`` → ``\Answered``; ``forward`` → the ``$Forwarded`` keyword.
"""

from __future__ import annotations

import logging

from .api import MailruApi, RemoteMessage
from .cache import Cache

log = logging.getLogger(__name__)


def _as_int(mailru_id: str) -> int | None:
    try:
        return int(mailru_id)
    except (TypeError, ValueError):
        return None


def imap_flags(m: RemoteMessage) -> set[str]:
    flags: set[str] = set()
    if not m.unread:
        flags.add("\\Seen")
    if m.flagged:
        flags.add("\\Flagged")
    if m.answered:
        flags.add("\\Answered")
    if m.forwarded:
        flags.add("$Forwarded")
    return flags


def _meta(m: RemoteMessage) -> dict:
    return {
        "subject": m.subject,
        "from": m.sender,
        "to": m.to,
        "cc": m.cc,
        "thread": m.thread_id,
        "attachments": m.has_attachments,
    }


class Syncer:
    def __init__(self, api: MailruApi, cache: Cache):
        self.api = api
        self.cache = cache

    async def sync_folders(self) -> None:
        for f in await self.api.folders():
            self.cache.upsert_folder(f.id, f.name, f.parent, f.type, f.system, f.total, f.unread)

    async def initial_sync(self, fid: str, depth: int = 2000, page: int = 200) -> int:
        """Load up to ``depth`` newest messages of a folder. Returns how many are now cached."""
        collected: list[RemoteMessage] = []
        offset = 0
        while len(collected) < depth:
            limit = min(page, depth - len(collected))
            result = await self.api.messages(fid, offset=offset, limit=limit)
            if not result.messages:
                break
            collected.extend(result.messages)
            offset += len(result.messages)
            if offset >= result.total:
                break
        # insert oldest-first (one transaction) so UID order matches arrival order
        self.cache.add_messages_batch(
            fid,
            [(m.id, m.date, m.size, imap_flags(m), _meta(m)) for m in reversed(collected)],
        )
        import time as _t

        self.cache.set_last_sync(fid, _t.time())
        log.info("initial sync %s: %d messages", fid, len(collected))
        return len(collected)

    async def incremental_sync(self, fid: str, window: int = 200, page: int = 200) -> tuple[int, list[int]]:
        """Refresh the newest ``window`` messages. Returns (new_count, expunged_uids).

        New messages are added with fresh UIDs; flags are refreshed; any cached message that falls
        inside the scanned window but is no longer on the server is marked deleted and its UID
        returned so the IMAP layer can report the expunge.
        """
        seen_remote: list[RemoteMessage] = []
        offset = 0
        total = None
        while len(seen_remote) < window:
            limit = min(page, window - len(seen_remote))
            result = await self.api.messages(fid, offset=offset, limit=limit)
            total = result.total
            if not result.messages:
                break
            seen_remote.extend(result.messages)
            offset += len(result.messages)
            if offset >= result.total:
                break

        remote_ids = {m.id for m in seen_remote}
        # Add new, oldest-first (one transaction) for tidy UID order; refresh flags on the rest.
        known = self.cache.live_mailru_ids(fid)
        fresh = [m for m in seen_remote if m.id not in known]
        new_count = self.cache.add_messages_batch(
            fid, [(m.id, m.date, m.size, imap_flags(m), _meta(m)) for m in reversed(fresh)]
        )
        for m in seen_remote:
            if m.id in known:
                uid = self.cache.uid_for(fid, m.id)
                if uid is not None:
                    self.cache.update_flags(fid, uid, imap_flags(m))

        # Deletion detection by id-range, NOT by Date header: Mail.ru ids are assigned in true
        # arrival order (the list is sorted id-desc), but Date headers arrive wildly out of order
        # (delayed/backdated delivery), so a date window spuriously expunges live messages and makes
        # them flap. We scanned the newest ids down to min_scanned_id; any cached message with an id
        # at or above that bound but absent from the scan is genuinely gone.
        expunged: list[int] = []
        scanned_ids = [_as_int(m.id) for m in seen_remote if _as_int(m.id) is not None]
        if scanned_ids:
            min_scanned_id = min(scanned_ids)
            for cm in self.cache.messages(fid):
                if cm.mailru_id in remote_ids:
                    continue
                cid = _as_int(cm.mailru_id)
                if cid is not None and cid >= min_scanned_id:
                    expunged.append(cm.uid)
            if expunged:
                self.cache.forget(fid, expunged)

        import time as _t

        self.cache.set_last_sync(fid, _t.time())
        if new_count or expunged:
            log.info("incremental %s: +%d new, -%d gone", fid, new_count, len(expunged))
        return new_count, expunged

    async def ensure_raw(self, fid: str, uid: int) -> bytes:
        """Return the RFC 822 source for a message, downloading and caching it on first use."""
        cached = self.cache.get_raw(fid, uid)
        if cached is not None:
            return cached
        cm = self.cache.message_by_uid(fid, uid)
        if cm is None:
            raise KeyError(f"no such uid {uid} in folder {fid}")
        raw = await self.api.download(cm.mailru_id, fid)
        from .mime import normalize

        raw = normalize(raw)
        self.cache.set_raw(fid, uid, raw)
        return raw
