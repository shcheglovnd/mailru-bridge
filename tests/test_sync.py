import asyncio
from unittest.mock import AsyncMock

import pytest

from mailru_bridge.api import MessagePage, RemoteFolder, RemoteMessage
from mailru_bridge.cache import Cache
from mailru_bridge.sync import Syncer


@pytest.fixture
def syncer(tmp_path):
    cache = Cache(tmp_path / "cache.sqlite3")
    cache.upsert_folder("0", "INBOX", "-1", "inbox", True, 0, 0)
    api = AsyncMock()
    yield Syncer(api, cache)
    cache.close()


def page(ids, total=None):
    return MessagePage([RemoteMessage(str(mid), "0", int(mid), unread=True) for mid in ids],
                       len(ids) if total is None else total, 0)


def test_empty_remote_folder_removes_all_cached_messages(syncer):
    syncer.cache.add_message("0", "10", 10, 1, set(), {})
    syncer.api.messages.return_value = page([])
    assert asyncio.run(syncer.incremental_sync("0")) == (0, [1])
    assert syncer.cache.counts("0") == (0, 0)


def test_complete_scan_removes_oldest_missing_message(syncer):
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    syncer.api.messages.return_value = page([30, 20])
    assert asyncio.run(syncer.incremental_sync("0")) == (0, [1])
    assert syncer.cache.live_mailru_ids("0") == {"20", "30"}


def test_partial_scan_keeps_messages_older_than_the_window(syncer):
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    syncer.api.messages.return_value = page([30, 20], total=10)
    assert asyncio.run(syncer.incremental_sync("0", window=2)) == (0, [])
    assert syncer.cache.live_mailru_ids("0") == {"10", "20", "30"}


def test_burst_larger_than_window_is_fetched_until_overlap(syncer):
    syncer.cache.add_message("0", "10", 10, 1, set(), {})
    syncer.api.messages.side_effect = [page([60, 50], 6), page([40, 30], 6), page([20, 10], 6)]
    new, gone = asyncio.run(syncer.incremental_sync("0", window=2, page=2))
    assert (new, gone) == (5, [])
    assert syncer.cache.live_mailru_ids("0") == {"10", "20", "30", "40", "50", "60"}
    assert syncer.api.messages.await_count == 3


def test_incremental_preserves_local_deleted_flag(syncer):
    syncer.cache.add_message("0", "10", 10, 1, {"\\Seen", "\\Deleted"}, {})
    syncer.api.messages.return_value = page([10])
    asyncio.run(syncer.incremental_sync("0"))
    assert syncer.cache.message_by_uid("0", 1).flags == {"\\Deleted"}


def test_deep_sync_keeps_canonical_size_and_deleted_flag(syncer):
    syncer.cache.add_message("0", "10", 10, 1, {"\\Deleted"}, {})
    raw = b"Subject: s\r\n\r\nbody\r\n"
    syncer.cache.set_raw("0", 1, raw)
    syncer.api.messages.return_value = page([10])
    asyncio.run(syncer.initial_sync("0", depth=10))
    assert syncer.cache.message_by_uid("0", 1).size == len(raw)
    assert syncer.cache.message_by_uid("0", 1).flags == {"\\Deleted"}


def test_deleted_folder_is_pruned_and_uidvalidity_changes_on_recreation(syncer, monkeypatch):
    monkeypatch.setattr("mailru_bridge.cache.time.time", lambda: 100)
    syncer.cache.upsert_folder("9", "Temporary", "-1", "user", False, 1, 1)
    validity = syncer.cache.folder("9")["uidvalidity"]
    syncer.cache.add_message("9", "10", 10, 1, set(), {})
    syncer.cache.set_meta("deep:9", "1")
    syncer.api.folders.return_value = [RemoteFolder("0", "INBOX")]
    asyncio.run(syncer.sync_folders())
    assert syncer.cache.folder("9") is None
    assert syncer.cache.messages("9") == []
    assert syncer.cache.get_meta("deep:9") == ""
    syncer.cache.upsert_folder("9", "Temporary", "-1", "user", False, 0, 0)
    assert syncer.cache.folder("9")["uidvalidity"] > validity


def test_changing_mailbox_during_pagination_does_not_expunge_cached_mail(syncer):
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    syncer.api.messages.side_effect = [page([40, 30], 4), page([10], 3)]
    new, gone = asyncio.run(syncer.incremental_sync("0", window=4, page=2))
    assert (new, gone) == (1, [])
    assert syncer.cache.live_mailru_ids("0") == {"10", "20", "30", "40"}


def test_mark_all_read_refreshes_entire_cache_without_downloading_history(syncer):
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, {"\\Deleted", "\\Flagged"}, {})
    before = [(m.uid, m.mailru_id) for m in syncer.cache.messages("0")]
    syncer.api.folders.return_value = [RemoteFolder("0", "INBOX", total=30, unread=0)]
    asyncio.run(syncer.sync_folders())
    assert syncer.cache.counts("0") == (3, 0)
    assert all(m.flags == {"\\Seen", "\\Deleted", "\\Flagged"} for m in syncer.cache.messages("0"))
    assert [(m.uid, m.mailru_id) for m in syncer.cache.messages("0")] == before
    syncer.api.messages.assert_not_awaited()
    syncer.api.download.assert_not_awaited()


def test_partial_read_counter_change_refreshes_flags_beyond_newest_window(syncer):
    syncer.cache.upsert_folder("0", "INBOX", "-1", "inbox", True, 3, 2)
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    syncer.api.folders.return_value = [RemoteFolder("0", "INBOX", total=3, unread=1)]
    newest = page([30, 20], 3)
    newest.messages[0].unread = False
    older = page([10], 3)
    older.messages[0].unread = False
    syncer.api.messages.side_effect = [newest, older]

    async def run():
        await syncer.sync_folders()
        assert syncer.cache.get_meta("flags_dirty:0") == "1"
        await syncer.incremental_sync("0", window=2, page=2)
    asyncio.run(run())
    assert syncer.cache.counts("0") == (3, 1)
    assert syncer.cache.message_by_uid("0", 1).seen
    assert syncer.api.messages.await_count == 2
    assert syncer.cache.get_meta("flags_dirty:0") == "0"


def test_periodic_refresh_detects_older_flag_changes_with_unchanged_unread_counter(syncer, monkeypatch):
    monkeypatch.setattr("mailru_bridge.sync.time.time", lambda: 1000)
    syncer.cache.set_meta("flags_sync:0", "1")
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    newest = page([30, 20], 3)
    older = page([10], 3)
    older.messages[0].unread = False
    syncer.api.messages.side_effect = [newest, older]
    asyncio.run(syncer.incremental_sync("0", window=2, page=2))
    assert syncer.cache.message_by_uid("0", 1).seen
    assert syncer.api.messages.await_count == 2
    assert syncer.cache.get_meta("flags_sync:0") == "1000"


def test_history_refresh_reaches_oldest_cached_message_after_new_arrivals(syncer):
    syncer.cache.add_message("0", "10", 10, 1, {"\\Deleted"}, {})
    syncer.cache.set_meta("flags_dirty:0", "1")
    old = page([10], 5)
    old.messages[0].unread = False
    syncer.api.messages.side_effect = [page([50, 40], 5), page([30, 20], 5), old]
    new, gone = asyncio.run(syncer.incremental_sync("0", window=2, page=2))
    assert (new, gone) == (4, [])
    assert syncer.cache.message_by_uid("0", 1).flags == {"\\Seen", "\\Deleted"}
    assert syncer.cache.message_by_uid("0", 1).mailru_id == "10"
    assert syncer.api.messages.await_count == 3


def test_routine_sync_keeps_small_window_before_full_refresh_is_due(syncer, monkeypatch):
    monkeypatch.setattr("mailru_bridge.sync.time.time", lambda: 1010)
    syncer.cache.set_meta("flags_sync:0", "1000")
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    syncer.api.messages.return_value = page([30, 20], 10)
    asyncio.run(syncer.incremental_sync("0", window=2, page=2))
    syncer.api.messages.assert_awaited_once_with("0", offset=0, limit=2)


def test_unstable_history_scan_keeps_pending_flag_refresh(syncer):
    for mid in [10, 20, 30]:
        syncer.cache.add_message("0", str(mid), mid, 1, set(), {})
    syncer.cache.set_meta("flags_dirty:0", "1")
    syncer.api.messages.side_effect = [page([30, 20], 3), page([10], 4)]
    asyncio.run(syncer.incremental_sync("0", window=2, page=2))
    assert syncer.cache.get_meta("flags_dirty:0") == "1"
