"""Request signatures used by the official Mail.ru Android app.

URL queries carry ``md5_signature`` and form bodies carry ``md5_post_signature``. Both
are the MD5 of the parameters, sorted by name and joined as ``k=v`` pairs with ``&``,
with a salt appended. The salt is a constant from the app (``UriSignCreator.SECRET_STRING``).
Values are hashed raw, not URL-encoded. Parameters with the same name keep their
original order; Python's sort is stable, so sorting by name alone gives that.
"""

import hashlib
from collections.abc import Iterable

SALT = "bb6b8b20-cbb6-43d4-bb77-749abf3e64db"

Params = list[tuple[str, str]]


def signature(params: Iterable[tuple[str, str]]) -> str:
    ordered = sorted(params, key=lambda kv: kv[0])
    unified = "&".join(f"{name}={value}" for name, value in ordered)
    return hashlib.md5((unified + SALT).encode("utf-8")).hexdigest()


def signed_query(params: Params) -> Params:
    return [*params, ("md5_signature", signature(params))]


def signed_form(fields: Params) -> Params:
    return [*fields, ("md5_post_signature", signature(fields))]
