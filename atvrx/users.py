"""Manage ATV-RX accounts.

    python -m atvrx.users add NAME          # asks for the password
    python -m atvrx.users add NAME --password-stdin < file
    python -m atvrx.users passwd NAME
    python -m atvrx.users remove NAME
    python -m atvrx.users list

Changes take effect in the running app at once; a changed or removed account is signed out.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .auth import AccountError, UserStore


def users_file() -> Path:
    return Path(os.environ.get("ATVRX_DATA", "/data")) / "users.json"


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise AccountError("The passwords do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atvrx.users", description="Manage ATV-RX accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("add", "passwd"):
        p = sub.add_parser(cmd)
        p.add_argument("name")
        p.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    sub.add_parser("remove").add_argument("name")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    store = UserStore(users_file())
    try:
        if args.cmd == "add":
            store.add(args.name, _password(args))
            print(f"Added {args.name}.")
        elif args.cmd == "passwd":
            store.set_password(args.name, _password(args))
            print(f"Changed the password of {args.name}.")
        elif args.cmd == "remove":
            store.remove(args.name)
            print(f"Removed {args.name}.")
        else:
            print("\n".join(store.names()) or "No users yet.")
    except AccountError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
