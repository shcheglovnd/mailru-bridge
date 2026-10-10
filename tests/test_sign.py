import hashlib

from mailru_bridge.sign import SALT, signature, signed_form, signed_query


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def test_signature_sorts_by_name_and_appends_salt():
    params = [("b", "2"), ("a", "1")]
    assert signature(params) == md5("a=1&b=2" + SALT)


def test_signature_is_order_independent_for_distinct_names():
    assert signature([("a", "1"), ("b", "2")]) == signature([("b", "2"), ("a", "1")])


def test_duplicate_names_keep_input_order():
    # Stable sort: same-named pairs stay in the order given.
    params = [("f", "10"), ("f", "5")]
    assert signature(params) == md5("f=10&f=5" + SALT)


def test_values_are_hashed_raw_not_url_encoded():
    params = [("q", "a b&c")]
    assert signature(params) == md5("q=a b&c" + SALT)


def test_signed_query_appends_md5_signature():
    params = [("a", "1")]
    out = signed_query(params)
    assert out[-1] == ("md5_signature", signature(params))
    assert out[:-1] == params


def test_signed_form_appends_md5_post_signature():
    fields = [("Login", "x"), ("Password", "y")]
    out = signed_form(fields)
    assert out[-1][0] == "md5_post_signature"
    assert out[-1][1] == signature(fields)
