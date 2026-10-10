"""HTTP client shared by login and API calls."""

from __future__ import annotations

import httpx

from .device import USER_AGENT
from .trace import Trace


def make_client(trace: Trace | None = None) -> httpx.Client:
    # Redirects are followed by hand: the SMS step ends in a redirect to the fake host
    # https://mobile-auth/..., which must be caught, not fetched.
    return httpx.Client(
        timeout=httpx.Timeout(30.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        event_hooks=trace.hooks() if trace else {},
    )
