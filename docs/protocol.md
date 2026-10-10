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
The app opens the `Continue` URL (with `client=mobile.app`) in a WebView; the page (a
"Ludwig" SPA) takes the SMS code and finally redirects to the sentinel
`https://mobile-auth/success`, where the app reads the tokens and a `tsa` cookie. The SPA's
internal calls are **not yet recovered**; confirm them from a `--trace` run and tighten
[`mailru_bridge/secondstep.py`](../mailru_bridge/secondstep.py). Statuses: `/success`,
`/fail`, `/error` (`ru.mail.ui.webview.handler.MobileAuthUrlHandler`).

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

Single-message read, flag/move and folder-list endpoints are obfuscated in the APK and are
to be confirmed by live probing (see [roadmap.md](roadmap.md)).
