import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from mailru_bridge.cache import Cache
from mailru_bridge.config import BridgeConfig
from mailru_bridge.service import BridgeService
from mailru_bridge.store import Account


def test_startup_failure_closes_api_and_cache(tmp_path):
    async def run():
        cache = Cache(tmp_path / "cache.sqlite3")
        service = BridgeService(Account("user@example.com"), BridgeConfig(), cache)
        service.imap.sync_folders = AsyncMock(side_effect=RuntimeError("upstream unavailable"))
        close = service.api.aclose
        service.api.aclose = AsyncMock(wraps=close)
        with pytest.raises(RuntimeError, match="upstream unavailable"):
            await service.run()
        service.api.aclose.assert_awaited_once()
        with pytest.raises(sqlite3.ProgrammingError):
            cache.folders()
    asyncio.run(run())


def test_listener_failure_cancels_other_tasks_before_closing_resources(tmp_path):
    async def run():
        cache = Cache(tmp_path / "cache.sqlite3")
        service = BridgeService(Account("user@example.com"), BridgeConfig(), cache)
        service.imap.sync_folders = AsyncMock()
        cancelled = asyncio.Event()

        async def other_server():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def failed_server():
            await asyncio.sleep(0)
            raise OSError("port in use")

        service.imap.serve = other_server
        service.smtp.serve = failed_server
        with pytest.raises(OSError, match="port in use"):
            await service.run()
        assert cancelled.is_set()
        assert all(task is asyncio.current_task() or task.done() for task in asyncio.all_tasks())
    asyncio.run(run())


def test_background_poll_refreshes_all_cached_folders_and_continues_after_one_failure(tmp_path):
    async def run():
        cache = Cache(tmp_path / "cache.sqlite3")
        for fid, name in [("0", "INBOX"), ("17", "Finance"), ("1", "Utilities"), ("9", "Unopened")]:
            cache.upsert_folder(fid, name, "-1", "user", False, 0, 0)
            if fid != "9":
                cache.set_last_sync(fid, 100)
        service = BridgeService(Account("user@example.com"), BridgeConfig(), cache)
        service.imap.sync_folders = AsyncMock()
        async def incremental(fid):
            if fid == "1":
                raise RuntimeError("temporary failure")
            return 0, []
        service.imap.incremental = AsyncMock(side_effect=incremental)
        try:
            await service._poll_cached_folders()
            service.imap.sync_folders.assert_awaited_once()
            assert {call.args[0] for call in service.imap.incremental.await_args_list} == {"0", "1", "17"}
        finally:
            await service.api.aclose()
            cache.close()
    asyncio.run(run())
