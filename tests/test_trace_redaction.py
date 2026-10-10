from mailru_bridge.trace import redact_json, redact_pairs, redact_url


def test_redact_pairs_masks_sensitive_values():
    out = dict(redact_pairs([("email", "x@example.com"), ("Password", "hunter2"), ("access_token", "abc")]))
    assert out["email"] == "x@example.com"
    assert "hunter2" not in out["Password"]
    assert "abc" not in out["access_token"]


def test_redact_url_masks_token_query():
    masked = redact_url("https://o2.mail.ru/token?client=mobile&refresh_token=SECRETVALUE123")
    assert "SECRETVALUE123" not in masked
    assert "client=mobile" in masked


def test_redact_json_masks_nested_tokens():
    out = redact_json({"body": {"access_token": "AAA", "email": "x@example.com"}})
    assert "AAA" not in out["body"]["access_token"]
    assert out["body"]["email"] == "x@example.com"


def test_redact_json_handles_lists():
    out = redact_json({"items": [{"token": "zzz"}, {"ok": 1}]})
    assert "zzz" not in out["items"][0]["token"]
    assert out["items"][1]["ok"] == 1
