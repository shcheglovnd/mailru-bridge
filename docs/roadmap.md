# Roadmap

### Done
- [x] Reverse-engineer the app API (auth, signing, device params, endpoints) — see
      [protocol.md](protocol.md).
- [x] Request signing (`md5_signature` / `md5_post_signature`) + unit tests.
- [x] Device profile (one stable fake phone per account).
- [x] State store: tokens + device, 0600, password never stored.
- [x] Redacted HTTP trace for debugging.
- [x] `login` (password + SMS second step) and `status` CLI; token refresh.

### Next (needs a live account)
- [x] Pin the SMS second-step exchange from the first traced login + the app decompile
      (`secstep` page JSON, `send_sms=1`, `AuthCode`/`csrf` POST, `mobile-auth/success`,
      trusted-`tsa` re-login). Implemented in `secondstep.py`; see `protocol.md`.
- [x] Live-confirmed the SMS exchange (2026-10-11): code accepted, success redirect gives an
      `AG_` token + trusted `tsa`. Finish with the **AG-token exchange** (not a password
      re-auth, which hits a reCAPTCHA) → `Authenticator.exchange_ag_token`.
- [x] Confirmed live (2026-10-11) the AG-token exchange returns real tokens — they arrive in an
      `oauth` wrapper with no `X-SWA-STATUS` header (`_token_container` handles all shapes).
- [x] Confirmed (2026-10-11): a saved trusted `tsa` cookie skips the SMS entirely — the
      next login is a single `POST /cgi-bin/auth` (password + `tsa`) → tokens, no second
      step and no captcha. Auth is complete end to end.
- [x] Folder list: `GET /api/v1/folders` (signed query + `access_token`) — confirmed live,
      schema documented in `protocol.md`.
- [x] Read path confirmed live (2026-10-11): per-folder list `messages/status`, and the **raw
      RFC822 source** via `messages/message/download` (`message/rfc822`). Flags/move/remove/folders
      and send pinned from the decompile. Documented in `protocol.md`.
- [x] API client (`api.py`): folders, list, raw fetch, marks, move, remove, folder CRUD, attach
      upload, send — async, one shared rate limiter, auto token refresh on `403 user`.

### Then
- [x] Local cache (`cache.py`, SQLite): stable IMAP UID ↔ Mail.ru id, flags, cached raw source.
- [x] Sync engine (`sync.py`): initial (bounded) + incremental, flag mapping, deletion detection.
- [x] Byte-exact MIME (`mime.py`) + IMAP `ENVELOPE`/`BODYSTRUCTURE` (`imap_struct.py`).
- [x] IMAP front end (`imap_server.py`): LOGIN/LIST/STATUS/SELECT/FETCH/STORE/SEARCH/MOVE/EXPUNGE/
      APPEND/IDLE, UID variants, modified UTF-7 names. Verified with `imaplib` against the live box.
- [x] SMTP front end (`smtp_server.py`) → `messages/send`; recipient routing incl. Bcc; attachments.
      Verified with `smtplib`.
- [x] Service runner (`service.py`) + `setup`/`serve` CLI; gentle background poll; `429` backoff.
- [x] **Live send confirmed** (2026-10-12): a self-addressed message sent via `messages/send`
      (returns `{"id":…}`), arrived in Inbox in ~5s, and the server filed the Sent copy itself.
      `send`/`draft` require a client-generated 32-char `id`; delete is permanent only from
      Trash/Spam, else move to Trash.
- [x] Draft APPEND via `messages/draft`, sharing SMTP's MIME/attachment conversion; saving never sends.
- [x] Run under systemd; reach it over WireGuard; TLS cert from the home CA.
- [x] Review fixes: authentication required for all mailbox commands, read-only enforcement, safe
      UID `*`/MOVE/EXPUNGE, multiple-client snapshots, local deletion marks preserved during sync,
      empty/deleted folders reconciled, SMTP transaction/DATA handling, safe mutation retry policy,
      credential-free command logging and saved-login reload. Regression tests include local sockets.
- [x] Web read-state sync across cached history: authoritative zero-unread reconciliation, broader
      scans on changed unread counts, periodic flag refresh, and background polling of all cached
      folders. Older flag updates are included in selected clients' IMAP notifications.
- [x] FairEmail refresh compatibility: received-date `BEFORE`/`ON`/`SINCE` searches, fresh arrivals
      published during SEARCH/FETCH, and immediate polling for manual SEARCH. Regression tests
      replay selecting a folder, a subsequent arrival, date SEARCH and FETCH over local sockets.
- [x] Richer SEARCH: sent dates, sender/recipient/subject/custom headers, decoded body/text and sizes;
      grouped literal arguments, charset errors and lazy source access with cheap filters first.
- [x] Further client compatibility fixes: SASL initial responses, correct subscription listings,
      mailbox case/hierarchy handling, deselection after failed SELECT, immediate destination-cache
      moves, edited-draft invalidation, cached header metadata, missing-message tolerance, MIME section
      NIL responses and RFC 2231 attachment filenames. Mail API list metadata requests plain text.
- [ ] Packaging and client setup docs for Thunderbird / FairEmail.

### Resolved questions
- Raw RFC822 source exists (`messages/message/download`) — no need to rebuild from JSON.
- Polling: incremental `messages/status` over a newest-first window; IDLE is poll-backed for now.
- Anti-fraud: keep one pinned device and a single rate-limited request stream (see `api.RateLimiter`).
