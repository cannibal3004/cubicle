"""Account administration from the server's shell, for when the UI cannot help:
a forgotten password, a lost authenticator, a token for a script.

    python -m app.cli users
    python -m app.cli set-password <username>        # reads the new password from stdin
    python -m app.cli disable-totp <username>
    python -m app.cli create-token <username> <name>  # prints the token once
    python -m app.cli create-user <username> [--role owner|member]   # password from stdin
"""

import argparse
import sys

from app.services.accounts import AccountError
from app.services.auth_service import auth_service


def _user_or_exit(username: str):
    user = auth_service.accounts.get_user_by_username(username)
    if user is None:
        print(f"No account named {username!r}.", file=sys.stderr)
        sys.exit(1)
    return user


def _password_from_stdin() -> str:
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("New password: ")
    return sys.stdin.readline().rstrip("\r\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("users", help="list accounts")
    p = sub.add_parser("set-password", help="set a user's password (stdin) and sign them out everywhere")
    p.add_argument("username")
    p = sub.add_parser("disable-totp", help="switch off two-factor for a user who lost their authenticator")
    p.add_argument("username")
    p = sub.add_parser("create-token", help="create an API token for a user; printed once")
    p.add_argument("username")
    p.add_argument("name")
    p = sub.add_parser("create-user", help="create an account (password from stdin)")
    p.add_argument("username")
    p.add_argument("--role", default="member", choices=["owner", "member"])
    args = parser.parse_args(argv)
    accounts = auth_service.accounts

    try:
        if args.command == "users":
            for user in accounts.list_users():
                flags = [f for f, on in (("2fa", user["totp_enabled"]), ("disabled", user["disabled"])) if on]
                print(f"{user['username']:<30} {user['role']:<8} {' '.join(flags)}")
            if accounts.setup_required():
                print("(setup not completed: no account has a password yet)")
        elif args.command == "set-password":
            user = _user_or_exit(args.username)
            accounts.set_password(user["id"], _password_from_stdin())
            print(f"Password set for {user['username']}; all sessions signed out.")
        elif args.command == "disable-totp":
            user = _user_or_exit(args.username)
            with accounts.database.connect() as connection:
                connection.execute(
                    "UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_pending = NULL, recovery_codes = NULL, totp_last_step = NULL WHERE id = ?",
                    (user["id"],),
                )
            print(f"Two-factor switched off for {user['username']}.")
        elif args.command == "create-token":
            user = _user_or_exit(args.username)
            _, token = accounts.create_token(user["id"], args.name)
            print(token)
        elif args.command == "create-user":
            user = accounts.create_user(args.username, _password_from_stdin(), role=args.role)
            print(f"Created {user['username']} ({user['role']}).")
    except AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
