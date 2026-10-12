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
- [ ] Confirm whether the saved trusted `tsa` lets a later login skip the SMS.
- [ ] Probe and document the read endpoints: folder list, message list, single-message
      fetch (incl. raw/RFC822 source if available), flags, move.
- [ ] API client: folders, list, fetch, flags, move, delete, send.

### Then
- [ ] IMAP front end backed by a local cache, syncing from the API.
- [ ] SMTP front end mapping submission to `messages/send`.
- [ ] Run as a service; gentle poll interval; backoff on `429`/captcha.
- [ ] Packaging and setup docs for Thunderbird / FairEmail.

### Open questions
- Is there a raw RFC822 source endpoint, or must messages be rebuilt from JSON + parts?
- Exact push/long-poll for new mail, to avoid tight polling.
- How aggressive is anti-fraud for a non-official client — keep traffic app-like.
