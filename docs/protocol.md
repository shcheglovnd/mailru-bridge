# Mail.ru mobile API — reverse-engineering notes

From static analysis of the official Android app `ru.mail.mailapp` 15.108.0.148470
(jadx decompile). These are notes, not an official spec; expect drift.

## Hosts

| Purpose | Host | TLS |
|---|---|---|
| Mail JSON API | `aj-https.mail.ru` (web alias `e.mail.ru`) | Let's Encrypt (public) |
| App default API | `alt-aj-https.mail.ru` | private **VK MAIL CA** — fails system TLS |
| Auth (SWA) | `auth.mail.ru` (app default `alt-auth.mail.ru`) | public / private |
| OAuth token | `o2.mail.ru` | public |
| Attachments | `alt-apf.mail.ru`, `alt-files.mail.ru` | |

Use the public-CA hosts (`aj-https.mail.ru`, `auth.mail.ru`, `o2.mail.ru`). The `alt-*`
hosts the app prefers use an internal CA and are not meant for third parties.

Unauthenticated `GET https://aj-https.mail.ru/api/v1/folders` returns
`{"status":403,"htmlencoded":true,"email":"…","body":"user"}` — the backend is reachable.

## Common request parameters

Every request carries the platform/device params the app appends
(`ru.mail.network.utils.client.platform.PlatformParams` + `DeviceInfoProvider`):

`mp=android`, `mmp=mail`, `client=mobile`, `DeviceID=<32 hex>`, `email`, `lang`,
`os=Android`, `os_version`, `ver=15.108.0.148470`, `appbuild=148470`, `vendor`, `model`,
`device_type=Smartphone`, `country`, `language`, `Lang`, `timezone` (`GMT±HH:MM`),
`device_name`, `playservices`.

User-Agent: `mobmail android 15.108.0.148470 ru.mail.mailapp`.

### Signature

- URL query gets `md5_signature`; form body gets `md5_post_signature`.
- Algorithm (`ru.mail.network.utils.sign.UriSignCreator`): take the pairs, sort by name
  (stable — equal names keep order), join as `k=v` with `&`, append the constant salt
  `bb6b8b20-cbb6-43d4-bb77-749abf3e64db`, MD5, lowercase hex. Values are hashed raw.
- The body signature is computed over the body params plus the URL query params.

Implemented in [`mailru_bridge/sign.py`](../mailru_bridge/sign.py).

## Authentication

### Password → tokens
`POST https://auth.mail.ru/cgi-bin/auth`, header `X-Mobile-App: e552fda2e6c711eaadc10242ac120002`.
Form: `Login`, `Password`, `client_id=mail-android`, `simple=1`, `mob_json=1`, `oauth2=1`,
`mobile=1`, `useragent=android`, `no_external_flow=1`, optional `bind_token`.
A trusted-device SMS can be skipped by sending a `tsa` cookie.

Outcome is in the **`X-SWA-STATUS`** response header
(`ru.mail.auth.request.AuthorizeRequestCommand`, codes in `…authdelegate.swa.SwaStatus`):

| Status | Meaning |
|---|---|
| 200 / none, body has tokens | success: `{"body":{"access_token","refresh_token"}}` |
| 808 | second step required (our case: SMS); body has `Continue` URL + `LudwigToken` |
| 706 | invalid login or password |
| 449 | captcha required |
| 429 | too many requests |
| 723 | IMAP redirect (settings in `X-SWA-IMAPSETTINGS`) |

### SMS second step
On `808` the app opens the `Continue` URL `https://auth.mail.ru/cgi-bin/secstep?client=mobile.app`
in a WebView. The page is a React app (`page2fa` bundle) whose state is embedded in the HTML
as JSON: `csrf`, `secstep_login`, `secstep_phone` (masked), `force_sms_send_url`,
`factor_choice_url`, `sms_enabled`, `ex_qrauth_enabled`, `ex_no_sms`. Recovered flow:

1. **Load** `GET …/cgi-bin/secstep?client=mobile.app` (WebView UA; carries the `secstep`
   cookie set on the `808`). Sets `secstep` + a first `tsa` cookie. **No SMS is sent here** —
   when more than one factor exists the page opens on a factor-choice screen.
2. **Send the code** `GET …/cgi-bin/secstep?client=mobile.app&send_sms=1` (`force_sms_send_url`).
   *This* is what dispatches the SMS. Re-read `csrf` from the returned page.
3. **Submit** `POST /cgi-bin/secstep` (plain form, `method=post`) with `csrf`, `Login`,
   `AuthCode=<code>`, `Permanent=1` ("trust this device"). Code field is `AuthCode` for SMS
   and TOTP, `calluiCode` for the call factor.
4. The server redirects (caught, not fetched) to
   `https://mobile-auth/success?token=AG_…&oauth2=1` and sets a now-**trusted** `tsa` cookie.
   `/fail` = wrong code, `/error` = server error (`ru.mail.ui.webview.handler.MobileAuthUrlHandler`,
   `…feature.common.webview.CommonWebViewClient`). That `token` is an **AuthGate (`AG_…`) token**,
   not an access token.
5. **Exchange the AG token for OAuth tokens**: `POST /cgi-bin/auth` with `Login`, `token=AG_…`,
   `client_id=mail-android`, **no password and no cookie**, plus the base fields
   `simple=1 mob_json=1 oauth2=1 migrant=0 migrant_native=0 ludwig=1`. Response carries **no
   `X-SWA-STATUS` header** and wraps the tokens in `oauth` (not `body`):
   `{"status":"ok","oauth":{"access_token","refresh_token","expires_in":86400}}`
   (confirmed live 2026-10-11). This is the app's
   `CommonAuthorizationMailApi.getMailAgTokenAuthorization(Login, token, client_id, @FieldMap)`
   (base fields from `BaseOauthParams`, call site `CommonAuthorizationRepository.getMailTokensByAgToken`).

> **Do not** finish by re-sending the password (even with the trusted `tsa` cookie): that path
> re-runs anti-fraud and is answered with an `808` whose `Continue` is a reCAPTCHA
> (`account.mail.ru/login?mode=authcaptcha`). Confirmed live on 2026-10-11: the AG-token
> exchange is the captcha-free completion.

Steps 1–5 are confirmed end to end from a live `--trace` run (SMS received, code accepted,
`AG_` token + trusted `tsa` returned, AG exchange returned a real access + refresh token). The
`tsa` cookie is long-lived (`expires` ≈ 90 days) and, **confirmed live 2026-10-11, skips the
second step entirely**: the next login is a single `POST /cgi-bin/auth` carrying `Password`
plus the `tsa` cookie, answered straight away with `{"status":"ok","oauth":{…}}` — no secstep,
no SMS, no captcha. Implemented in
[`secondstep.py`](../mailru_bridge/secondstep.py) + `Authenticator.exchange_ag_token`.

### Refresh
`POST https://o2.mail.ru/token`, form `client_id=mail-android`, `grant_type=refresh_token`,
`refresh_token`. Response `{"access_token", "refresh_token"}`.

## Mail API (session = `access_token` query param)

The "Tornado" session adds `access_token=<oauth access>` to the query
(`ru.mail.serverapi.TornadoSession`); legacy sessions use an `Mpop` cookie + `token` param.

Endpoints confirmed from `@UrlPath` / Retrofit annotations:

| Path (`POST` unless noted) | Purpose |
|---|---|
| `api/v1/m/threads/status/smart` | list threads/messages per folder (`folders=[{folder,offset,limit}]`, `last_modified`, `snippet_limit`) |
| `api/v1/messages/send` | send a message |
| `api/v1/messages/draft`, `.../schedule` | drafts / scheduled send |
| `api/v1/messages/remove` | delete messages |
| `api/v1/messages/services/unspam` | not-spam |
| `api/v1/m/threads/remove`, `.../services/unspam` | thread delete / not-spam |
| `api/v1/folders/clear` | empty a folder |
| `api/v1/filters` | filters |
| `api/v1/messages/attaches/get` | fetch an attachment |

### Folder list — confirmed live (2026-10-11)

`GET https://aj-https.mail.ru/api/v1/folders` with the standard platform params + `email` +
`lang` + `access_token`, all `md5_signature`-signed in the query (no body, no post-signature).
Returns `{"status":200,"htmlencoded":true,"email":…,"last_modified":<unix>,"body":[ folder… ]}`.

Each folder object carries:
`id` (string; system folders use fixed ids, custom folders small integers), `name`, `parent`,
`child`, `children`, `type`, `system`, `open`, `favorite`, `archive`, `security`, `share`,
`only_web`, `new_cnt`, `last_visit`, `size`, `cloud_stock_size`, and the counters
`messages_total`, `messages_unread`, `messages_flagged`, `messages_pinned`, `messages_snoozed`,
`messages_with_attachments`.

Fixed system-folder ids (same for every account): `0` Inbox, `500000` Sent, `500001` Drafts,
`500002` Trash, `500010` Archive, `500011` Social, `950` Spam.

Single-message read and flag/move endpoints are obfuscated in the APK and are still to be
confirmed by live probing (see [roadmap.md](roadmap.md)).
