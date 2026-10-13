"""SMS (and call/TOTP) second-step completion.

After a password login returns :class:`~mailru_bridge.auth.SecondStepChallenge`, the
official app opens the ``Continue`` URL (``/cgi-bin/secstep``) in a WebView. That page is a
React app ("fauclient") whose state — a ``csrf`` token, the masked phone, and the URL that
actually dispatches the SMS — is embedded in the HTML. Crucially the code is **not** sent
just by loading the page: when an account has more than one factor (SMS, QR, call, TOTP)
the page opens on a factor-choice screen, and the SMS goes out only when the user picks
"by phone number", which navigates to ``force_sms_send_url`` (``…&send_sms=1``).

So this module:

1. loads the secstep page and reads its embedded state,
2. GETs ``send_sms=1`` to make Mail.ru send the code,
3. POSTs the code back to ``/cgi-bin/secstep`` as the page's form does
   (fields ``csrf``, ``Login``, ``AuthCode``, ``Permanent``),
4. lets the server redirect to the app sentinel ``https://mobile-auth/success`` and, on
   the way, set a *trusted-device* ``tsa`` cookie.

The caller exchanges the sentinel's AG token for OAuth tokens using
``Authenticator.exchange_ag_token``. The trusted-device ``tsa`` cookie is long-lived, so later
password logins can skip the SMS entirely.

Field names and the flow are taken from the decompiled app (``CommonAuthorizationMailApi``,
``CommonWebViewClient``, ``MobileAuthUrlHandler``) and from the page bundle ``page2fa``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from .device import Device
from .trace import Trace

# The WebView treats this fake host as the end of the flow (MobileAuthUrlHandler).
MOBILE_AUTH_HOST = "mobile-auth"
SECSTEP_PATH = "/cgi-bin/secstep"
# Used if the page does not spell out force_sms_send_url for some reason.
DEFAULT_SMS_SEND_URL = f"{SECSTEP_PATH}?client=mobile.app&send_sms=1"


class SecondStepError(Exception):
    """The second step could not be completed (wrong code, server error, parse failure)."""


@dataclass
class SecstepPage:
    """Parsed state of a ``/cgi-bin/secstep`` page."""

    response: httpx.Response
    csrf: str
    login: str = ""
    phone: str = ""
    sms_send_url: str = DEFAULT_SMS_SEND_URL
    factor_choice_url: str = ""
    sms_enabled: bool = True

    @property
    def url(self) -> str:
        return str(self.response.url)


@dataclass
class SecondStepOutcome:
    """Result of a completed second step. ``tsa_cookie`` unlocks SMS-free re-login."""

    tsa_cookie: str
    additional_params: dict = field(default_factory=dict)
    success_url: str = ""


def _field(html: str, key: str) -> str | None:
    """Read a single ``"key":"value"`` string from the embedded JSON state."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if not m:
        return None
    try:
        return json.loads('"' + m.group(1) + '"')  # unescape \/ \u.... etc.
    except ValueError:
        return m.group(1)


def _flag(html: str, key: str) -> bool | None:
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"?(\d+)"?', html)
    return bool(int(m.group(1))) if m else None


class SecondStepFlow:
    """Drives the ``/cgi-bin/secstep`` page the way the app's WebView does."""

    def __init__(self, client: httpx.Client, device: Device, trace: Trace | None = None):
        self.client = client
        self.device = device
        self.trace = trace

    # -- steps -------------------------------------------------------------

    def open_challenge(self, continue_url: str) -> SecstepPage:
        """Load the second-step page (following in-site redirects) and parse its state."""
        resp = self._follow_get(continue_url)
        page = self._parse(resp)
        if self.trace:
            self.trace.note("secstep.open", has_csrf=bool(page.csrf), sms_enabled=page.sms_enabled)
        return page

    def request_sms(self, page: SecstepPage) -> SecstepPage:
        """Ask Mail.ru to actually send the SMS, then re-read the (fresh) page state."""
        url = str(httpx.URL(page.url).join(page.sms_send_url))
        resp = self._follow_get(url)
        fresh = self._parse(resp)
        # Keep the phone/urls if the reloaded page omits them.
        fresh.phone = fresh.phone or page.phone
        fresh.sms_send_url = fresh.sms_send_url or page.sms_send_url
        if self.trace:
            self.trace.note("secstep.request_sms", status=resp.status_code)
        return fresh

    def submit_code(self, page: SecstepPage, email: str, code: str, permanent: bool = True) -> SecondStepOutcome:
        """POST the code as the page's form does and wait for the success sentinel."""
        if not page.csrf:
            raise SecondStepError(
                "No csrf token on the second-step page; re-run with --trace and inspect it."
            )
        data = {
            "csrf": page.csrf,
            "Login": page.login or email,
            "AuthCode": code,
            "Permanent": "1" if permanent else "0",
        }
        url = str(httpx.URL(page.url).join(SECSTEP_PATH))
        resp = self.client.post(url, data=data, headers=self._headers())
        return self._await_sentinel(resp)

    # -- helpers -----------------------------------------------------------

    def _headers(self) -> dict:
        return {"User-Agent": self.device.webview_user_agent}

    def _follow_get(self, url: str, limit: int = 10) -> httpx.Response:
        """GET ``url`` following redirects by hand, stopping before the fake sentinel host."""
        for _ in range(limit):
            resp = self.client.get(url, headers=self._headers())
            if not resp.is_redirect:
                return resp
            nxt = httpx.URL(str(resp.url)).join(resp.headers.get("location", ""))
            if nxt.host == MOBILE_AUTH_HOST:
                return resp
            url = str(nxt)
        raise SecondStepError("Too many redirects loading the second-step page.")

    def _await_sentinel(self, resp: httpx.Response, limit: int = 10) -> SecondStepOutcome:
        """Follow redirects after the code POST until the ``mobile-auth`` sentinel."""
        for _ in range(limit):
            if resp.is_redirect:
                nxt = httpx.URL(str(resp.url)).join(resp.headers.get("location", ""))
                if nxt.host == MOBILE_AUTH_HOST:
                    return self._outcome(nxt)
                resp = self.client.get(str(nxt), headers=self._headers())
                continue
            # No redirect: the page re-rendered, which for this flow means the code was
            # rejected (or an unknown state). Surface a clear, actionable error.
            raise SecondStepError(
                "The SMS code was not accepted (no success redirect). Check the code and, "
                "if it was correct, re-run with --trace so the flow can be inspected."
            )
        raise SecondStepError("Too many redirects after submitting the code.")

    def _outcome(self, sentinel: httpx.URL) -> SecondStepOutcome:
        path = sentinel.path
        if path.endswith("/fail"):
            raise SecondStepError("Wrong or expired SMS code.")
        if path.endswith("/error"):
            raise SecondStepError("Mail.ru reported an internal error on the second step.")
        if not path.endswith("/success"):
            raise SecondStepError(f"Unexpected second-step result path: {path!r}.")
        tsa = self.client.cookies.get("tsa") or ""
        if not tsa:
            raise SecondStepError(
                "Reached the success sentinel but no trusted-device (tsa) cookie was set; "
                "re-run with --trace and inspect the cookies."
            )
        params = dict(sentinel.params)
        if self.trace:
            self.trace.note("secstep.success", params=list(params.keys()))
        return SecondStepOutcome(tsa_cookie=tsa, additional_params=params, success_url=str(sentinel))

    def _parse(self, resp: httpx.Response) -> SecstepPage:
        html = resp.text
        csrf = _field(html, "csrf") or ""
        return SecstepPage(
            response=resp,
            csrf=csrf,
            login=_field(html, "secstep_login") or "",
            phone=_field(html, "secstep_phone") or "",
            sms_send_url=_field(html, "force_sms_send_url") or DEFAULT_SMS_SEND_URL,
            factor_choice_url=_field(html, "factor_choice_url") or "",
            sms_enabled=_flag(html, "sms_enabled") is not False,
        )
