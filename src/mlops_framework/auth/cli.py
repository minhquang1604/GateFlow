"""Mint, list and revoke API keys from a shell.

Exists to solve the bootstrap: ``/api/api-keys`` requires the ``admin``
scope, so the *first* key cannot be created through it. This talks to
the database directly — the same trust boundary as running a migration,
and available in exactly the same places (a deployed container's shell,
a maintenance task) rather than needing a credential to get a
credential.

    python -m mlops_framework.auth.cli create alice --scopes admin
    python -m mlops_framework.auth.cli list
    python -m mlops_framework.auth.cli revoke alice

The plaintext is printed once, by ``create``, and is unrecoverable
afterwards.
"""

from __future__ import annotations

import argparse
import sys

from mlops_framework.auth.manager import VALID_SCOPES, ApiKeyManager
from mlops_framework.database.session import get_db_manager
from mlops_framework.exceptions import ApiKeyError


def _create(args: argparse.Namespace) -> int:
    with get_db_manager().get_session() as session:
        try:
            minted = ApiKeyManager(session).create(
                name=args.name,
                scopes=args.scopes,
                description=args.description,
            )
        except ApiKeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"created API key {minted.name!r} with scopes {minted.scopes}")
        print()
        print(f"    {minted.plaintext}")
        print()
        print("This is the only time it will be shown. Store it now.")
    return 0


def _list(args: argparse.Namespace) -> int:
    with get_db_manager().get_session() as session:
        rows = ApiKeyManager(session).list_keys(include_revoked=args.all)
        if not rows:
            print("no API keys")
            return 0
        print(f"{'NAME':<24} {'PREFIX':<18} {'SCOPES':<22} STATE")
        for row in rows:
            state = f"revoked {row.revoked_at:%Y-%m-%d}" if row.revoked_at else "active"
            scopes = (row.scopes_json or "[]").strip("[]").replace('"', "")
            print(f"{row.name:<24} {row.key_prefix + '…':<18} {scopes:<22} {state}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    with get_db_manager().get_session() as session:
        try:
            row = ApiKeyManager(session).revoke(args.name)
        except ApiKeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"revoked {row.name!r} at {row.revoked_at}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mlops-api-keys", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="mint a key and print it once")
    create.add_argument("name", help="the principal this key acts as (lands in AuditLog.actor)")
    create.add_argument(
        "--scopes",
        nargs="+",
        default=["read"],
        choices=sorted(VALID_SCOPES),
        help="one or more of: " + ", ".join(sorted(VALID_SCOPES)),
    )
    create.add_argument("--description", default=None)
    create.set_defaults(func=_create)

    listing = sub.add_parser("list", help="list keys (never prints a usable key)")
    listing.add_argument("--all", action="store_true", help="include revoked keys")
    listing.set_defaults(func=_list)

    revoke = sub.add_parser("revoke", help="revoke a key by name")
    revoke.add_argument("name")
    revoke.set_defaults(func=_revoke)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
