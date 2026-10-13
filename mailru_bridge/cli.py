"""Command line: log in (interactive) and check the stored session.

The password is read with getpass and used only for the login request; it is never written
to disk or into the trace. Only tokens and the device profile are stored, under
``~/.local/state/mailru-bridge`` with owner-only permissions.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import __version__
from .auth import Authenticator, AuthError, SecondStepChallenge, apply_tokens
from .http import make_client
from .secondstep import SecondStepError, SecondStepFlow
from .store import Account
from .trace import Trace


def _login(args: argparse.Namespace) -> int:
    email = args.email or input("Mail.ru e-mail: ").strip()
    account = Account.load()
    if account is None or account.email != email:
        account = Account(email=email)
    # Persist the device profile before the first login attempt. The server must see the
    # SAME phone (DeviceID) on every retry — a fresh DeviceID each time looks like repeated
    # logins from new devices and trips Mail.ru's anti-fraud (captcha). No secret is stored.
    account.save()

    trace = Trace("login") if args.trace else None
    try:
        with make_client(trace) as client:
            auth = Authenticator(client, account.device)
            password = getpass.getpass("Password: ")
            try:
                result = auth.start_password(email, password, account.tsa_cookie)

                if isinstance(result, SecondStepChallenge):
                    flow = SecondStepFlow(client, account.device, trace)
                    page = flow.open_challenge(result.continue_url)
                    page = flow.request_sms(page)  # this is what actually sends the SMS
                    print(f"SMS code sent to {page.phone or 'your phone'}.", file=sys.stderr)
                    code = input("SMS code: ").strip()
                    outcome = flow.submit_code(page, email, code)
                    if outcome.tsa_cookie:
                        # Persist the trusted-device cookie the moment we earn it, before the
                        # token exchange can fail — it is what lets later logins skip the SMS,
                        # and a fresh SMS is expensive (anti-fraud, captcha).
                        account.tsa_cookie = outcome.tsa_cookie
                        account.save()
                    ag_token = outcome.additional_params.get("token", "")
                    if not ag_token:
                        raise AuthError(
                            "Second step succeeded but the success URL carried no AG token; "
                            "re-run with --trace and inspect it."
                        )
                    # Trade the AG token for OAuth tokens (no password, no cookie) — this is
                    # how the app finishes, and it avoids the captcha a password re-auth hits.
                    client.cookies.clear()
                    tokens = auth.exchange_ag_token(email, ag_token)
                    apply_tokens(account, tokens)
                else:
                    apply_tokens(account, result)
            finally:
                del password

        account.save()
    except (AuthError, SecondStepError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        if trace:
            print(f"Trace saved in {trace.dir}", file=sys.stderr)
        return 1
    finally:
        if trace:
            trace.close()

    print(f"Logged in as {email}. Session saved to {Account.path()}.")
    if trace:
        print(f"Trace saved in {trace.dir}")
    return 0


def _status(_args: argparse.Namespace) -> int:
    account = Account.load()
    if account is None:
        print("No stored session. Run `mailru-bridge login`.")
        return 1
    state = "valid" if account.access_token_valid else "needs refresh"
    print(f"Account: {account.email}")
    print(f"Device:  {account.device.device_name} (DeviceID {account.device.device_id[:8]}…)")
    print(f"Access token: {state}")
    print(f"Refresh token: {'present' if account.refresh_token else 'missing'}")
    print(f"Trusted-device cookie: {'present' if account.tsa_cookie else 'none'}")
    return 0


def _setup(args: argparse.Namespace) -> int:
    """Configure how the local IMAP/SMTP servers listen and the password clients use."""
    from .config import BridgeConfig, hash_password

    account = Account.load()
    if account is None:
        print("Log in first: `mailru-bridge login`.", file=sys.stderr)
        return 1
    cfg = BridgeConfig.load() or BridgeConfig()
    cfg.username = args.username or cfg.username or account.email
    if args.imap_port is not None:
        cfg.imap_port = args.imap_port
    if args.smtp_port is not None:
        cfg.smtp_port = args.smtp_port
    if args.bind:
        cfg.bind_host = args.bind
    if args.tls_cert:
        cfg.tls_cert = args.tls_cert
    if args.tls_key:
        cfg.tls_key = args.tls_key
    if args.poll is not None:
        cfg.poll_seconds = args.poll
    if args.initial_depth is not None:
        cfg.initial_depth = args.initial_depth
    if args.sync_window is not None:
        cfg.sync_window = args.sync_window
    if args.flag_refresh is not None:
        cfg.flag_refresh_seconds = args.flag_refresh
    if args.alert_command is not None:
        cfg.alert_command = args.alert_command
    if not cfg.password_hash or args.password:
        pw = getpass.getpass("Password for mail clients to use with the bridge: ")
        again = getpass.getpass("Repeat: ")
        if pw != again:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        cfg.password_hash = hash_password(pw)
    try:
        cfg.save()
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    print(f"Bridge configured. Clients log in as {cfg.username!r}.")
    print(f"  IMAP: {cfg.bind_host}:{cfg.imap_port}   SMTP: {cfg.bind_host}:{cfg.smtp_port}   TLS: {cfg.tls}")
    print("Start it with `mailru-bridge serve`.")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import asyncio
    import logging

    from .config import BridgeConfig
    from .service import BridgeService

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs the full request URL at INFO, which includes the access_token and DeviceID in the
    # query. Keep those out of the journal: only warnings and errors from the HTTP layer.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    account = Account.load()
    if account is None:
        print("Log in first: `mailru-bridge login`.", file=sys.stderr)
        return 1
    cfg = BridgeConfig.load()
    if cfg is None or not cfg.password_hash:
        print("Run `mailru-bridge setup` first to set the bridge password.", file=sys.stderr)
        return 1
    try:
        service = BridgeService(account, cfg)
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailru-bridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="log in and store the session")
    p_login.add_argument("--email", help="mailbox address (prompted if omitted)")
    p_login.add_argument("--trace", action="store_true", help="record a redacted HTTP trace")
    p_login.set_defaults(func=_login)

    p_status = sub.add_parser("status", help="show the stored session")
    p_status.set_defaults(func=_status)

    p_setup = sub.add_parser("setup", help="configure the IMAP/SMTP servers and client password")
    p_setup.add_argument("--username", help="what mail clients log in as (default: your address)")
    p_setup.add_argument("--password", action="store_true", help="(re)set the client password")
    p_setup.add_argument("--bind", help="address to listen on (default 127.0.0.1)")
    p_setup.add_argument("--imap-port", type=int, help="IMAP port (default 1143)")
    p_setup.add_argument("--smtp-port", type=int, help="SMTP port (default 1025)")
    p_setup.add_argument("--tls-cert", help="PEM certificate for implicit TLS")
    p_setup.add_argument("--tls-key", help="PEM private key for implicit TLS")
    p_setup.add_argument("--poll", type=int, help="background poll interval, seconds")
    p_setup.add_argument("--initial-depth", type=int,
                         help="newest messages to sync per folder on first open (default 2000)")
    p_setup.add_argument("--sync-window", type=int,
                         help="newest messages re-checked each incremental poll (default 200)")
    p_setup.add_argument("--flag-refresh", type=int,
                         help="periodic refresh of flags across cached history, seconds (default 900)")
    p_setup.add_argument("--alert-command",
                         help="command run (message appended) when a re-login is needed; "
                              "pass '' to disable")
    p_setup.set_defaults(func=_setup)

    p_serve = sub.add_parser("serve", help="run the IMAP/SMTP bridge")
    p_serve.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p_serve.set_defaults(func=_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
