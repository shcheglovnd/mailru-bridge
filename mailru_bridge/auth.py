"""Login and token refresh against the Mail.ru auth backend.

Replicates the official app's ``AuthorizeRequestCommand`` (``ru.mail.auth.request``):

* Password login is a form ``POST`` to ``auth.mail.ru/cgi-bin/auth`` with ``oauth2=1``,
  so the response body is ``{"body": {"access_token", "refresh_token"}}``.
* The outcome is carried in the ``X-SWA-STATUS`` response header (see :class:`SwaStatus`).
  ``808`` means a second step (our case: an SMS code) is required; the JSON body then
  holds a ``Continue`` URL and a ``LudwigToken``.
* An access token is refreshed at ``o2.mail.ru/token`` with ``grant_type=refresh_token``.

The second-step (SMS) exchange is a web page meant for a WebView;
:meth:`Authenticator.start_password` surfaces the challenge so the CLI can complete it using
``secondstep.py``, then exchange the returned AG token for OAuth credentials.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from .device import USER_AGENT, X_MOBILE_APP, Device
from .sign import signed_query
from .store import Account

AUTH_HOST = "auth.mail.ru"
OAUTH_HOST = "o2.mail.ru"
CLIENT_ID = "mail-android"
# Lifetime the app assumes for an access token before refreshing (seconds).
ACCESS_TTL = 60 * 60


class SwaStatus:
    OK = 200
    CAPTCHA_REQUIRED = 449
    TOO_MANY_REQUESTS = 429
    INVALID_LOGIN_OR_PASSWORD = 706
    SECOND_STEP_REQUIRED = 808


class AuthError(Exception):
    """Login failed for a reason the user must act on."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class SecondStepChallenge:
    """An SMS second step the user must satisfy to finish logging in."""

    continue_url: str
    ludwig_token: str


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int | None = None


def _base_query(device: Device, email: str) -> list[tuple[str, str]]:
    return [("email", email), ("lang", device.language), *device.platform_params()]


class Authenticator:
    def __init__(self, client: httpx.Client, device: Device):
        self.client = client
        self.device = device

    # -- password step -----------------------------------------------------

    def start_password(
        self, email: str, password: str, tsa_cookie: str = ""
    ) -> Tokens | SecondStepChallenge:
        """First login step. Returns tokens, or a challenge if an SMS code is needed."""
        url = httpx.URL(
            f"https://{AUTH_HOST}/cgi-bin/auth",
            params=signed_query(_base_query(self.device, email)),
        )
        form = {
            "Login": email,
            "Password": password,
            "client_id": CLIENT_ID,
            "simple": "1",
            "mob_json": "1",
            "oauth2": "1",
            "mobile": "1",
            "useragent": "android",
            "no_external_flow": "1",
        }
        headers = {"User-Agent": USER_AGENT, "X-Mobile-App": X_MOBILE_APP}
        cookies = {"tsa": tsa_cookie} if tsa_cookie else None
        resp = self.client.post(url, data=form, headers=headers, cookies=cookies)
        return self._read_password_response(resp)

    def _read_password_response(self, resp: httpx.Response) -> Tokens | SecondStepChallenge:
        status = self._swa_status(resp)
        body = self._json(resp)

        if status in (SwaStatus.OK, None) and self._has_tokens(body):
            return self._tokens(body)

        if status == SwaStatus.SECOND_STEP_REQUIRED:
            cont = body.get("Continue") or body.get("continue") or ""
            if not cont:
                raise AuthError(
                    "Second step required but no Continue URL in the response "
                    "(trace the run and inspect it).",
                    status,
                )
            if "captcha" in cont.lower():
                raise AuthError(
                    "Mail.ru is demanding a captcha instead of the SMS step — its anti-fraud "
                    "was tripped by repeated login attempts (a changing device made each one "
                    "look like a new phone). Your device profile is now pinned; wait ~30–60 "
                    "minutes without any login attempts, then try once more.",
                    SwaStatus.CAPTCHA_REQUIRED,
                )
            url = str(httpx.URL(cont).copy_merge_params({"client": "mobile.app"}))
            return SecondStepChallenge(url, body.get("LudwigToken", ""))

        if status == SwaStatus.INVALID_LOGIN_OR_PASSWORD:
            raise AuthError("Invalid login or password.", status)
        if status == SwaStatus.CAPTCHA_REQUIRED:
            raise AuthError("Mail.ru is asking for a captcha; try again later.", status)
        if status == SwaStatus.TOO_MANY_REQUESTS:
            raise AuthError("Too many login attempts; wait before retrying.", status)
        raise AuthError(f"Login failed (SWA status {status}).", status)

    def exchange_ag_token(self, email: str, ag_token: str) -> Tokens:
        """Finish a 2FA login by trading the second step's AG token for OAuth tokens.

        The SMS step ends at ``https://mobile-auth/success?token=AG_…``; that ``token`` is an
        "AuthGate" token, not an access token. The app exchanges it with a dedicated call —
        ``POST /cgi-bin/auth`` carrying ``token`` instead of ``Password`` and **no cookie**
        (``CommonAuthorizationMailApi.getMailAgTokenAuthorization``; base fields from
        ``BaseOauthParams``). Re-sending the password here instead would re-run anti-fraud and
        can be answered with a captcha, which is what happened before this path existed.
        """
        url = httpx.URL(
            f"https://{AUTH_HOST}/cgi-bin/auth",
            params=signed_query(_base_query(self.device, email)),
        )
        form = {
            "Login": email,
            "token": ag_token,
            "client_id": CLIENT_ID,
            "simple": "1",
            "mob_json": "1",
            "oauth2": "1",
            "migrant": "0",
            "migrant_native": "0",
            "ludwig": "1",
        }
        headers = {"User-Agent": USER_AGENT, "X-Mobile-App": X_MOBILE_APP}
        resp = self.client.post(url, data=form, headers=headers)
        result = self._read_password_response(resp)
        if isinstance(result, SecondStepChallenge):
            raise AuthError(
                "AG-token exchange unexpectedly asked for another step; inspect the trace."
            )
        return result

    # -- token refresh -----------------------------------------------------

    def refresh(self, refresh_token: str) -> Tokens:
        url = httpx.URL(
            f"https://{OAUTH_HOST}/token",
            params=signed_query(self.device.platform_params()),
        )
        form = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = self.client.post(url, data=form, headers={"User-Agent": USER_AGENT})
        body = self._json(resp)
        container = self._token_container(body)
        if resp.status_code != 200 or container is None:
            raise AuthError(
                f"Token refresh failed (HTTP {resp.status_code}); re-run login.",
                resp.status_code,
            )
        return Tokens(
            container["access_token"],
            container.get("refresh_token", refresh_token),
            container.get("expires_in"),
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _swa_status(resp: httpx.Response) -> int | None:
        raw = resp.headers.get("X-SWA-STATUS")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return -1

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _token_container(body: dict) -> dict | None:
        """Find the dict that holds the tokens across the backend's response shapes.

        A direct password login answers ``{"body": {access_token, …}}``; the AG-token
        exchange answers ``{"status": "ok", "oauth": {access_token, refresh_token,
        expires_in}}``; the refresh endpoint answers flat. Check each in turn.
        """
        for key in ("oauth", "body"):
            inner = body.get(key)
            if isinstance(inner, dict) and "access_token" in inner:
                return inner
        if "access_token" in body:
            return body
        return None

    @staticmethod
    def _has_tokens(body: dict) -> bool:
        return Authenticator._token_container(body) is not None

    @staticmethod
    def _tokens(body: dict) -> Tokens:
        inner = Authenticator._token_container(body) or {}
        return Tokens(
            inner["access_token"],
            inner.get("refresh_token", ""),
            inner.get("expires_in"),
        )


def apply_tokens(account: Account, tokens: Tokens) -> None:
    account.access_token = tokens.access_token
    if tokens.refresh_token:
        account.refresh_token = tokens.refresh_token
    ttl = tokens.expires_in if tokens.expires_in else ACCESS_TTL
    account.access_expires_at = time.time() + ttl


def ensure_access_token(client: httpx.Client, account: Account) -> str:
    """Return a valid access token, refreshing and persisting it if needed."""
    if account.access_token_valid:
        return account.access_token
    if not account.refresh_token:
        raise AuthError("No refresh token; run `mailru-bridge login` first.")
    tokens = Authenticator(client, account.device).refresh(account.refresh_token)
    apply_tokens(account, tokens)
    account.save()
    return account.access_token
