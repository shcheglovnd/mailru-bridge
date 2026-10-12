"""Local mailbox cache: the bridge between Mail.ru's string message ids and IMAP's integer UIDs.

IMAP requires, per mailbox, a stable ``UIDVALIDITY`` and monotonic 32-bit ``UID``s that are never
reused, plus message sequence numbers. Mail.ru identifies a message by a 64-bit-ish decimal string
and has no notion of either. This cache (one SQLite file) assigns a local UID to each Mail.ru id the
first time it is seen in a folder and remembers it, so a given message keeps its UID across syncs.

It also stores the last-known flags and header metadata, and the raw RFC 822 source once fetched, so
repeated IMAP reads do not re-hit the API. The cache is the source of truth the IMAP front end
serves from; a separate sync task keeps it current.

Access is synchronous (SQLite) behind one lock; the async server calls into it via
``asyncio.to_thread``. One connection, WAL mode, is plenty for a single user with a few clients.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .store import state_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id            TEXT PRIMARY KEY,         -- Mail.ru folder id (string)
    name          TEXT NOT NULL,
    parent        TEXT NOT NULL DEFAULT '-1',
    type          TEXT,
    system        INTEGER NOT NULL DEFAULT 0,
    uidvalidity   INTEGER NOT NULL,
    uidnext       INTEGER NOT NULL DEFAULT 1,
    total         INTEGER NOT NULL DEFAULT 0,
    unread        INTEGER NOT NULL DEFAULT 0,
    last_sync     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    folder_id     TEXT NOT NULL,
    uid           INTEGER NOT NULL,
    mailru_id     TEXT NOT NULL,
    date          INTEGER NOT NULL DEFAULT 0,
    size          INTEGER NOT NULL DEFAULT 0,
    flags         TEXT NOT NULL DEFAULT '',  -- space-separated IMAP flags we track
    deleted       INTEGER NOT NULL DEFAULT 0,
    meta          TEXT,                      -- JSON: subject, from, to (for ENVELOPE w/o full fetch)
    raw           BLOB,                      -- RFC822 source, once downloaded
    PRIMARY KEY (folder_id, uid)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_mailru ON messages(folder_id, mailru_id);
CREATE INDEX IF NOT EXISTS ix_msg_uid ON messages(folder_id, uid);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class CachedMessage:
    uid: int
    mailru_id: str
    date: int
    size: int
    flags: set[str]
    deleted: bool
    meta: dict

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags


def _flags_to_text(flags: set[str]) -> str:
    return " ".join(sorted(flags))


def _text_to_flags(text: str) -> set[str]:
    return set(text.split()) if text else set()


class Cache:
    def __init__(self, path: Path | None = None):
        self.path = path or (state_dir() / "cache.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- folders ---------------------------------------------------------------------------

    def upsert_folder(self, fid: str, name: str, parent: str, ftype: str, system: bool,
                      total: int, unread: int) -> None:
        with self._lock:
            row = self._conn.execute("SELECT uidvalidity FROM folders WHERE id=?", (fid,)).fetchone()
            if row is None:
                uidvalidity = int(time.time())
                self._conn.execute(
                    "INSERT INTO folders(id,name,parent,type,system,uidvalidity,total,unread) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (fid, name, parent, ftype, int(system), uidvalidity, total, unread),
                )
            else:
                self._conn.execute(
                    "UPDATE folders SET name=?,parent=?,type=?,system=?,total=?,unread=? WHERE id=?",
                    (name, parent, ftype, int(system), total, unread, fid),
                )
            self._conn.commit()

    def folders(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM folders ORDER BY id").fetchall()

    def folder(self, fid: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()

    def set_last_sync(self, fid: str, when: float) -> None:
        with self._lock:
            self._conn.execute("UPDATE folders SET last_sync=? WHERE id=?", (when, fid))
            self._conn.commit()

    # -- messages --------------------------------------------------------------------------

    def uid_for(self, fid: str, mailru_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT uid FROM messages WHERE folder_id=? AND mailru_id=?", (fid, mailru_id)
            ).fetchone()
            return row["uid"] if row else None

    def add_message(self, fid: str, mailru_id: str, date: int, size: int, flags: set[str],
                    meta: dict) -> int:
        """Assign and return a UID for a newly seen message (or the existing one)."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT uid FROM messages WHERE folder_id=? AND mailru_id=?", (fid, mailru_id)
            ).fetchone()
            if existing:
                # refresh flags/meta/size; keep the uid
                self._conn.execute(
                    "UPDATE messages SET date=?,size=?,flags=?,meta=?,deleted=0 "
                    "WHERE folder_id=? AND mailru_id=?",
                    (date, size, _flags_to_text(flags), json.dumps(meta, ensure_ascii=False),
                     fid, mailru_id),
                )
                self._conn.commit()
                return existing["uid"]
            row = self._conn.execute("SELECT uidnext FROM folders WHERE id=?", (fid,)).fetchone()
            uid = row["uidnext"] if row else 1
            self._conn.execute(
                "INSERT INTO messages(folder_id,uid,mailru_id,date,size,flags,meta) "
                "VALUES(?,?,?,?,?,?,?)",
                (fid, uid, mailru_id, date, size, _flags_to_text(flags),
                 json.dumps(meta, ensure_ascii=False)),
            )
            self._conn.execute("UPDATE folders SET uidnext=? WHERE id=?", (uid + 1, fid))
            self._conn.commit()
            return uid

    def update_flags(self, fid: str, uid: int, flags: set[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET flags=? WHERE folder_id=? AND uid=?",
                (_flags_to_text(flags), fid, uid),
            )
            self._conn.commit()

    def set_raw(self, fid: str, uid: int, raw: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET raw=?,size=? WHERE folder_id=? AND uid=?",
                (raw, len(raw), fid, uid),
            )
            self._conn.commit()

    def get_raw(self, fid: str, uid: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT raw FROM messages WHERE folder_id=? AND uid=?", (fid, uid)
            ).fetchone()
            return bytes(row["raw"]) if row and row["raw"] is not None else None

    def mark_deleted(self, fid: str, uids: list[int]) -> None:
        with self._lock:
            self._conn.executemany(
                "UPDATE messages SET deleted=1 WHERE folder_id=? AND uid=?",
                [(fid, u) for u in uids],
            )
            self._conn.commit()

    def forget(self, fid: str, uids: list[int]) -> None:
        """Remove messages from the cache entirely (e.g. expunged, or gone from the server)."""
        with self._lock:
            self._conn.executemany(
                "DELETE FROM messages WHERE folder_id=? AND uid=?", [(fid, u) for u in uids]
            )
            self._conn.commit()

    def live_mailru_ids(self, fid: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mailru_id FROM messages WHERE folder_id=? AND deleted=0", (fid,)
            ).fetchall()
            return {r["mailru_id"] for r in rows}

    def messages(self, fid: str, include_deleted: bool = False) -> list[CachedMessage]:
        with self._lock:
            q = "SELECT * FROM messages WHERE folder_id=?"
            if not include_deleted:
                q += " AND deleted=0"
            q += " ORDER BY uid"
            rows = self._conn.execute(q, (fid,)).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def message_by_uid(self, fid: str, uid: int) -> CachedMessage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE folder_id=? AND uid=?", (fid, uid)
            ).fetchone()
        return self._row_to_msg(row) if row else None

    def counts(self, fid: str) -> tuple[int, int]:
        """(exists, unseen) for a mailbox, over non-deleted messages."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE folder_id=? AND deleted=0", (fid,)
            ).fetchone()["c"]
            unseen = self._conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE folder_id=? AND deleted=0 "
                "AND instr(flags, '\\Seen')=0",
                (fid,),
            ).fetchone()["c"]
        return total, unseen

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> CachedMessage:
        return CachedMessage(
            uid=row["uid"],
            mailru_id=row["mailru_id"],
            date=row["date"],
            size=row["size"],
            flags=_text_to_flags(row["flags"]),
            deleted=bool(row["deleted"]),
            meta=json.loads(row["meta"]) if row["meta"] else {},
        )

    # -- small key/value for sync bookkeeping ----------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )
            self._conn.commit()
