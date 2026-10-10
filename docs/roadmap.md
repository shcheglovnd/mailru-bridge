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
- [ ] First traced login to pin the SMS second-step exchange, then tighten
      `secondstep.py` to it.
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
