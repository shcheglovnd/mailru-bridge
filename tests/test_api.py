import asyncio
import time
from unittest.mock import Mock

import httpx
import pytest

from mailru_bridge.api import ApiError, AuthExpired, MailruApi, RateLimited, RateLimiter
from mailru_bridge.store import Account


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILRU_BRIDGE_STATE", str(tmp_path))
    return Account("user@example.com", access_token="test-access", refresh_token="test-refresh",
                   access_expires_at=time.time() + 3600)


def run_api(account, handler, action, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = MailruApi(account, client=client, limiter=RateLimiter(0, 0), **kwargs)
            return await action(api)
    return asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "http500", "json500"])
def test_mutations_are_not_replayed_after_ambiguous_failure(account, failure):
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("contains-secret-query-token", request=request)
        if failure == "http500":
            return httpx.Response(500)
        return httpx.Response(200, json={"status": 500, "body": {"access_token": "secret"}})

    with pytest.raises(ApiError) as exc:
        run_api(account, handle, lambda api: api._call("api/v1/messages/send", form=[("id", "test")]))
    assert len(requests) == 1
    assert "contains-secret-query-token" not in str(exc.value)
    assert "secret" not in str(exc.value)


def test_safe_read_retries_a_temporary_error(account, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr("mailru_bridge.api.asyncio.sleep", AsyncMock())
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(503) if count == 1 else httpx.Response(200, json={"status": 200, "body": []})

    assert run_api(account, handle, lambda api: api.folders()) == []
    assert count == 2


@pytest.mark.parametrize("status, error", [(429, RateLimited), (503, ApiError), (200, ApiError)])
def test_temporary_refresh_failure_does_not_trigger_relogin_alert(account, status, error):
    notify = Mock()
    with pytest.raises(error) as exc:
        run_api(account, lambda req: httpx.Response(status, json={}),
                lambda api: api.refresh_token(stale=account.access_token), on_auth_expired=notify)
    assert not isinstance(exc.value, AuthExpired)
    notify.assert_not_called()


def test_revoked_refresh_token_triggers_alert(account):
    notify = Mock()
    with pytest.raises(AuthExpired):
        run_api(account, lambda req: httpx.Response(400, json={"error": "invalid_grant"}),
                lambda api: api.refresh_token(stale=account.access_token), on_auth_expired=notify)
    notify.assert_called_once()


def test_running_api_adopts_tokens_saved_by_login(account):
    account.access_expires_at = 0
    newer = Account(account.email, device=account.device, access_token="new-access",
                    refresh_token="new-refresh", access_expires_at=time.time() + 3600)
    newer.save()
    handle = Mock(side_effect=AssertionError("fresh credentials should skip refresh request"))
    assert run_api(account, handle, lambda api: api.refresh_token()) == "new-access"
    assert account.refresh_token == "new-refresh"
    handle.assert_not_called()


def test_bad_folder_listing_is_not_treated_as_empty(account):
    with pytest.raises(ApiError):
        run_api(account, lambda req: httpx.Response(200, json={"status": 200, "body": {}}),
                lambda api: api.folders())


@pytest.mark.parametrize("body", [{"messages": []}, {"messages": {}, "messages_total": 0},
                                  {"messages": [], "messages_total": -1},
                                  {"messages": [None], "messages_total": 1}])
def test_malformed_message_listing_is_rejected_before_cache_reconciliation(account, body):
    with pytest.raises(ApiError):
        run_api(account, lambda req: httpx.Response(200, json={"status": 200, "body": body}),
                lambda api: api.messages("0"))


def test_rate_limit_penalty_delays_the_next_request():
    limiter = RateLimiter(0, 0)
    before = time.monotonic()
    limiter.penalize(30)
    assert limiter._next >= before + 30


def test_listing_requests_plain_metadata_without_html_entities(account):
    def handle(request):
        assert request.url.params["htmlencoded"] == "false"
        if request.url.path.endswith("folders"):
            return httpx.Response(200, json={"status": 200, "htmlencoded": False,
                                           "body": [{"id": "0", "name": "A & B"}]})
        return httpx.Response(200, json={"status": 200, "htmlencoded": False, "body": {
            "messages_total": 1, "messages": [{"id": "100", "subject": "A & B"}]}})
    assert run_api(account, handle, lambda api: api.folders())[0].name == "A & B"
    assert run_api(account, handle, lambda api: api.messages("0")).messages[0].subject == "A & B"
