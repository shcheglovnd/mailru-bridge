# mailru-bridge

Standard **IMAP/SMTP** access to a free **Mail.ru / VK Почта** mailbox, for desktop and
mobile mail clients (Thunderbird, FairEmail, …).

Since **12 June 2026** Mail.ru serves IMAP/POP3/SMTP only to the paid *Mail Space* plan;
the web interface and the official mobile app stay free. This bridge talks to the same
private JSON API the Android app uses and re-exposes the mailbox over plain IMAP and SMTP
on your own machine, so existing clients keep working.

> **Status: working.** Login (password + SMS, then token refresh), a local IMAP server (folders,
> message list, raw-source fetch, flags, move, delete, search, IDLE) and an SMTP submission server
> are implemented and tested against a live account. See [docs/protocol.md](docs/protocol.md) for
> the reverse-engineered API and [docs/roadmap.md](docs/roadmap.md) for what remains.

## How it works

```
mail client ──IMAP/SMTP──▶ mailru-bridge ──HTTPS JSON API──▶ Mail.ru
 (Thunderbird,                (this project)                  (aj-https.mail.ru,
  FairEmail)                                                   auth.mail.ru, o2.mail.ru)
```

The bridge logs in once the way the app does, stores the resulting OAuth **refresh token**
(never your password), and refreshes an access token as needed. It presents the mailbox to
local clients; reads, moves and deletes are mapped back onto the API, and outgoing mail is
submitted through the API's send endpoint.

## Install

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Log in

Run it on the machine that will host the bridge. You are asked for your e-mail, password
(read without echo) and, if two-factor is on, the SMS code:

```sh
mailru-bridge login            # add --trace on first run to record a redacted HTTP trace
mailru-bridge status
```

- Your **password is never stored** and never written to the trace — only tokens and a
  generated device profile are saved, under `~/.local/state/mailru-bridge/` (mode 0600).
- A successful SMS step also stores a *trusted-device* cookie, so later logins can skip the
  SMS.

## Run the bridge

Set the password your mail clients will use with the bridge (this is **not** your Mail.ru
password, which the bridge never stores), then start the servers:

```sh
mailru-bridge setup          # prompts for a client password; sets ports (IMAP 1143, SMTP 1025)
mailru-bridge serve          # runs the IMAP + SMTP servers and a gentle background sync
```

`setup` options: `--bind` (default `127.0.0.1`), `--imap-port`, `--smtp-port`, `--tls-cert` /
`--tls-key` (PEM, for implicit TLS), `--poll` (background sync seconds), `--username`.

### Point a mail client at it

Use your Mail.ru address as the username and the **bridge** password you just set:

| | Server | Port | Security |
|---|---|---|---|
| IMAP | your bridge host | 1143 | STARTTLS/SSL if you set a cert, else none (VPN only) |
| SMTP | your bridge host | 1025 | same |

Let the **server manage Sent and Drafts** (the API files sent mail in Sent itself). The first time
a large folder is opened the bridge syncs its newest messages (configurable depth) into a local
cache; later opens are served from the cache and only new mail is fetched.

What works: folder list, message list, reading (headers shown without downloading the whole
message; the full source is fetched once when you open a message and then cached), mark read/unread
and flagged, move, delete (to Trash), create/rename/delete folders, IDLE, and sending with
attachments. `COPY` is not supported (Mail.ru has no copy — use move); arbitrary `APPEND` is limited
(Sent/Drafts are server-managed).

## Privacy & safety

This talks to an undocumented API as if it were the official app, so treat it gently:

- **No private data in the repo.** Tokens, traces and mail are kept in the state directory
  outside the checkout and are covered by `.gitignore`. A `pre-commit` hook
  (`git config core.hooksPath .githooks`) blocks commits that contain Mail.ru addresses,
  token-like strings or phone numbers; add your own patterns in
  `~/.config/mailru-bridge/private-patterns`.
- **Don't hammer the server.** The bridge presents one device and polls gently, to avoid
  tripping anti-fraud and risking a block. Pick a calm poll interval.
- Traces are redacted (passwords/tokens/cookies masked) but can still contain your address
  and one-time values, so keep them private.

## Remote access from your phone

Don't expose the bridge to the internet directly. Reach it over a VPN to your home network
(e.g. WireGuard), the same as any other internal service.

## Legal

Independent project; not affiliated with VK or Mail.ru. For interoperability with your own
account. Review the Mail.ru terms of service and use at your own risk. MIT licensed.
