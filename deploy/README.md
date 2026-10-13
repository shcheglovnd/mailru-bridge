# Deploying the bridge

1. **Log in and configure** (on the host that will run it):
   ```sh
   mailru-bridge login            # one-time; password + SMS, then token refresh
   mailru-bridge setup            # set the password mail clients will use
   ```
   For TLS, point `--tls-cert`/`--tls-key` at a PEM cert+key for the hostname your
   clients use (a private CA your devices already trust works well):
   ```sh
   mailru-bridge setup --bind 0.0.0.0 --imap-port 993 --smtp-port 465 \
       --tls-cert /path/fullchain.pem --tls-key /path/privkey.pem
   ```

2. **Run as a service.** `mailru-bridge.service` here is a systemd template; install it
   per user with `mailru-bridge@<user>.service`, or copy it and hard-code `User=`/paths:
   ```sh
   sudo cp mailru-bridge.service /etc/systemd/system/mailru-bridge@.service
   sudo systemctl enable --now mailru-bridge@youruser
   ```
   The template allows writes to `/home/<user>/.local/state/mailru-bridge` under
   `ProtectSystem=strict`. If you override the state location, adjust `ReadWritePaths` too.

3. **Reach it safely.** Bind to localhost or an internal address and reach it over a VPN
   (e.g. WireGuard); do not expose IMAP/SMTP to the internet. If clients off the LAN must
   resolve the hostname, point their VPN DNS at your resolver.

4. **Client settings.** IMAP on your chosen port (SSL/TLS), SMTP likewise; username is your
   mailbox address, password is the bridge password from step 1. Let the **server** manage
   the Sent folder (the API files sent mail itself). Disable client Sent APPEND. Draft APPEND is
   saved through `messages/draft` and preserves attachments; it does not send the message.

## Alert on auth failure (optional)

The bridge lives on the saved refresh token; if Mail.ru ever revokes it, syncing stops until you
re-run `mailru-bridge login`. The running bridge adopts the new credentials on its next refresh;
there is no need to restart after re-login to the same account/device. Temporary API outages and
rate limits are reported as upstream failures, without requesting a new login.
Point `alert_command` at a notifier and the bridge will ping you
(at most hourly) when that happens:

```sh
mailru-bridge setup --alert-command /path/to/notify   # pass '' to disable
```

The command is parsed with shell-style quoting and run without a shell, with the alert message as
its final argument; a notifier that runs longer than 30 seconds is stopped. Keep any notifier secrets (a bot
token, a webhook URL) inside that script or its own config, never in this repo.
