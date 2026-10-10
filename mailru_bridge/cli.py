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

    trace = Trace("login") if args.trace else None
    try:
        with make_client(trace) as client:
            auth = Authenticator(client, account.device)
            password = getpass.getpass("Password: ")
            result = auth.start_password(email, password, account.tsa_cookie)
            del password

            if isinstance(result, SecondStepChallenge):
                print("An SMS code was sent to your phone.", file=sys.stderr)
                flow = SecondStepFlow(client, auth, trace)
                page = flow.open_challenge(result.continue_url)
                code = input("SMS code: ").strip()
                outcome = flow.submit_code(page, code)
                apply_tokens(account, outcome.tokens)
                if outcome.tsa_cookie:
                    account.tsa_cookie = outcome.tsa_cookie
            else:
                apply_tokens(account, result)

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
