"""SMS second-step completion.

After a password login returns :class:`~mailru_bridge.auth.SecondStepChallenge`, the
official app opens the ``Continue`` URL in a WebView. That page (a "Ludwig" SPA on
``auth.mail.ru``) collects the SMS code, verifies it, and finally redirects to the
sentinel ``https://mobile-auth/success`` carrying the session; on it the app reads the
account tokens and a ``tsa`` ("trusted device") cookie that lets later logins skip the SMS.

The SPA's internal calls are not recovered from static analysis, so this module drives the
flow against the live page: it follows the Continue URL, then submits the code to the form
or JSON endpoint discovered there. Run the first login with ``--trace`` so the exact
exchange is captured and this module can be tightened to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .auth import Authenticator, Tokens
from .device import USER_AGENT
from .trace import Trace

MOBILE_AUTH_SENTINEL = "mobile-auth"


@dataclass
class SecondStepOutcome:
    tokens: Tokens
    tsa_cookie: str


class SecondStepError(Exception):
    pass


class SecondStepFlow:
    """Walks the Continue URL to the point where the SMS code is accepted.

    This is deliberately thin and defensive: it follows redirects by hand (so the final
    ``mobile-auth`` redirect is caught, not fetched) and keeps every cookie the pages set.
    The code-submission shape is confirmed on the first live, traced run.
    """

    def __init__(self, client: httpx.Client, auth: Authenticator, trace: Trace | None = None):
        self.client = client
        self.auth = auth
        self.trace = trace

    def open_challenge(self, continue_url: str) -> httpx.Response:
        """Load the second-step page, following in-site redirects and keeping cookies."""
        url = continue_url
        for _ in range(10):
            resp = self.client.get(url, headers={"User-Agent": self.auth.device.webview_user_agent})
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if MOBILE_AUTH_SENTINEL in location:
                    return resp
                url = str(httpx.URL(url).join(location))
                continue
            return resp
        raise SecondStepError("Too many redirects loading the second-step page.")

    def submit_code(self, page: httpx.Response, code: str) -> SecondStepOutcome:
        """Submit the SMS code. Confirmed/adjusted from the first traced live run."""
        action, fields = self._find_form(page)
        if action is None:
            raise SecondStepError(
                "Could not locate the code form on the second-step page. "
                "Re-run with --trace and share the captured page so the flow can be completed."
            )
        fields["code"] = code
        url = str(httpx.URL(str(page.url)).join(action))
        resp = self.client.post(
            url, data=fields, headers={"User-Agent": self.auth.device.webview_user_agent}
        )
        return self._capture_result(resp)

    def _capture_result(self, resp: httpx.Response) -> SecondStepOutcome:
        for _ in range(10):
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if MOBILE_AUTH_SENTINEL in location:
                    tsa = self._cookie("tsa")
                    tokens = self._tokens_from_redirect(httpx.URL(location))
                    if tokens is None:
                        raise SecondStepError(
                            "Reached mobile-auth but found no tokens; inspect the trace."
                        )
                    return SecondStepOutcome(tokens, tsa)
                resp = self.client.get(
                    str(httpx.URL(str(resp.url)).join(location)),
                    headers={"User-Agent": self.auth.device.webview_user_agent},
                )
                continue
            raise SecondStepError(f"Second step did not reach mobile-auth (HTTP {resp.status_code}).")
        raise SecondStepError("Too many redirects after submitting the code.")

    def _tokens_from_redirect(self, url: httpx.URL) -> Tokens | None:
        params = dict(url.params)
        if "access_token" in params:
            return Tokens(params["access_token"], params.get("refresh_token", ""))
        return None

    def _cookie(self, name: str) -> str:
        return self.client.cookies.get(name, "") or ""

    @staticmethod
    def _find_form(page: httpx.Response) -> tuple[str | None, dict[str, str]]:
        html = page.text
        form = re.search(r"<form[^>]*>.*?</form>", html, re.I | re.S)
        if not form:
            return None, {}
        block = form.group(0)
        action = re.search(r'action\s*=\s*["\']([^"\']+)["\']', block, re.I)
        fields: dict[str, str] = {}
        for m in re.finditer(r"<input\b[^>]*>", block, re.I):
            tag = m.group(0)
            name = re.search(r'name\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if not name:
                continue
            value = re.search(r'value\s*=\s*["\']([^"\']*)["\']', tag, re.I)
            fields[name.group(1)] = value.group(1) if value else ""
        return (action.group(1) if action else ""), fields
