# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Command-line surface for the ad-hoc ``gerrit_api`` client.

Owns argument-parser construction, logging setup and sub-command
dispatch for the small diagnostic CLI exposed by :mod:`gerrit_api`.
Keeping it separate lets the library module stay free of ``argparse``
and ``print`` concerns; :func:`gerrit_api.main` wires the two together.

The import of :class:`gerrit_api.GerritDevClient` is deliberately
``TYPE_CHECKING``-only so this module never imports its own caller at
runtime.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gerrit_api import GerritDevClient


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``gerrit_api`` CLI."""
    parser = argparse.ArgumentParser(
        description="Gerrit API client for DEVELOPMENT_BECOME_ANY_ACCOUNT mode"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080",
        help="Gerrit base URL",
    )
    parser.add_argument(
        "--account-id",
        type=int,
        default=1000000,
        help="Account ID to become",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # whoami command
    subparsers.add_parser("whoami", help="Show current account info")

    # create-user command
    create_parser = subparsers.add_parser("create-user", help="Create a user account")
    create_parser.add_argument("username", help="Username to create")
    create_parser.add_argument("--name", help="Full name")
    create_parser.add_argument("--email", help="Email address")
    create_parser.add_argument("--ssh-key", help="SSH public key to add")
    create_parser.add_argument(
        "--admin",
        action="store_true",
        help="Add to Administrators group",
    )

    # add-ssh-key command
    ssh_parser = subparsers.add_parser("add-ssh-key", help="Add SSH key to account")
    ssh_parser.add_argument("account", help="Account username or ID")
    ssh_parser.add_argument("ssh_key", help="SSH public key")

    return parser


def configure_logging(verbose: bool) -> None:
    """Configure root logging for the CLI."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def dispatch(client: GerritDevClient, args: argparse.Namespace) -> None:
    """Run the selected sub-command against an authenticated *client*.

    Args:
        client: A client that has already become the requested account.
        args: Parsed arguments from :func:`build_parser`.
    """
    if args.command == "whoami":
        account = client.get_account("self")
        print(json.dumps(account, indent=2))

    elif args.command == "create-user":
        ssh_keys = [args.ssh_key] if args.ssh_key else []
        account = client.setup_user_with_ssh_keys(
            username=args.username,
            ssh_keys=ssh_keys,
            name=args.name,
            email=args.email,
            add_to_admins=args.admin,
        )
        print(json.dumps(account, indent=2))

    elif args.command == "add-ssh-key":
        result = client.add_ssh_key(args.account, args.ssh_key)
        print(json.dumps(result, indent=2))
