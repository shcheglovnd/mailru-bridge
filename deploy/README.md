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

3. **Reach it safely.** Bind to localhost or an internal address and reach it over a VPN
   (e.g. WireGuard); do not expose IMAP/SMTP to the internet. If clients off the LAN must
   resolve the hostname, point their VPN DNS at your resolver.

4. **Client settings.** IMAP on your chosen port (SSL/TLS), SMTP likewise; username is your
   mailbox address, password is the bridge password from step 1. Let the **server** manage
   the Sent and Drafts folders (the API files sent mail itself).

## Alert on auth failure (optional)

The bridge lives on the saved refresh token; if Mail.ru ever revokes it, syncing stops until you
re-run `mailru-bridge login`. Point `alert_command` at a notifier and the bridge will ping you
(at most hourly) when that happens:

```sh
mailru-bridge setup --alert-command /path/to/notify   # pass '' to disable
```

The command is run with the alert message as its final argument. Keep any notifier secrets (a bot
token, a webhook URL) inside that script or its own config, never in this repo.
