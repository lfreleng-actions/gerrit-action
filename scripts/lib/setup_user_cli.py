# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Command-line argument parser for the user setup entry point.

Split out of ``setup-gerrit-user.py`` so the option definitions stay
separate from the run sequences that consume them.  Every name here is
re-exported from ``setup-gerrit-user.py``.
"""

from __future__ import annotations

import argparse
import os


def build_parser(epilog: str | None = None) -> argparse.ArgumentParser:
    """Build the argument parser for the user setup script.

    Args:
        epilog: Text shown after the option list in ``--help``.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description="Set up Gerrit users with SSH keys",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    _add_connection_args(parser)
    _add_multi_instance_args(parser)
    _add_user_args(parser)
    _add_ssh_key_args(parser)
    _add_output_args(parser)

    return parser


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add the options that select which Gerrit endpoint to talk to."""
    conn_group = parser.add_argument_group("Connection")
    conn_group.add_argument(
        "--url",
        default=os.environ.get("GERRIT_URL", "http://localhost:8080"),
        help="Gerrit base URL (default: $GERRIT_URL or http://localhost:8080)",
    )
    conn_group.add_argument(
        "--container",
        help="Docker container name (uses container's mapped port)",
    )
    conn_group.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port for container (default: 8080)",
    )


def _add_multi_instance_args(parser: argparse.ArgumentParser) -> None:
    """Add the options for the mode that replaces add-ssh-auth-keys.sh."""
    multi_group = parser.add_argument_group("Multi-instance mode")
    multi_group.add_argument(
        "--instances-file",
        help=(
            "Path to instances.json file. When combined with "
            "--loop-instances, iterates over all instances and "
            "configures SSH keys for each one."
        ),
    )
    multi_group.add_argument(
        "--loop-instances",
        action="store_true",
        help=(
            "Loop over all instances in the instances file "
            "(requires --instances-file). Replaces add-ssh-auth-keys.sh."
        ),
    )
    multi_group.add_argument(
        "--use-api-path",
        action="store_true",
        default=os.environ.get("USE_API_PATH", "false").lower() == "true",
        help=(
            "Use the api_path from instance metadata when building "
            "Gerrit URLs (default: $USE_API_PATH or false)"
        ),
    )


def _add_user_args(parser: argparse.ArgumentParser) -> None:
    """Add the options describing the account to create or update."""
    user_group = parser.add_argument_group("User")
    user_group.add_argument(
        "--username",
        "-u",
        default=os.environ.get("SSH_AUTH_USERNAME", "admin"),
        help="Username to create/update (default: $SSH_AUTH_USERNAME or 'admin')",
    )
    user_group.add_argument(
        "--name",
        "-n",
        help="Full name for the account (default: same as username)",
    )
    user_group.add_argument(
        "--email",
        "-e",
        help="Email address (default: username@example.com)",
    )
    user_group.add_argument(
        "--no-admin",
        action="store_true",
        help="Don't add user to Administrators group",
    )


def _add_ssh_key_args(parser: argparse.ArgumentParser) -> None:
    """Add the options that supply SSH public keys."""
    ssh_group = parser.add_argument_group("SSH Keys")
    ssh_group.add_argument(
        "--ssh-key",
        "-k",
        action="append",
        dest="ssh_keys",
        help="SSH public key (can be specified multiple times)",
    )
    ssh_group.add_argument(
        "--ssh-key-file",
        "-f",
        action="append",
        dest="ssh_key_files",
        help="File containing SSH public key(s) (can be specified multiple times)",
    )


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    """Add the verbosity and output-format options."""
    output_group = parser.add_argument_group("Output")
    output_group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    output_group.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output",
    )
    output_group.add_argument(
        "--json",
        action="store_true",
        help="Output account info as JSON",
    )
