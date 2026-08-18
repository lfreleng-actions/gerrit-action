#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
CLI script for setting up Gerrit users with SSH keys.

This script provides a command-line interface for setting up Gerrit users
in DEVELOPMENT_BECOME_ANY_ACCOUNT mode. It supports:

- Creating user accounts
- Adding SSH keys from files or strings
- Adding users to the Administrators group
- Running against local or containerized Gerrit instances
- Looping over all instances from an instances.json file (replaces
  add-ssh-auth-keys.sh)

Usage:
    # Setup user with SSH key from file
    ./setup-gerrit-user.py --url http://localhost:8080 \\
        --username testuser \\
        --ssh-key-file ~/.ssh/id_ed25519.pub

    # Setup user with SSH key from environment variable
    SSH_AUTH_KEYS="ssh-ed25519 AAAA... user@host" \\
    ./setup-gerrit-user.py --url http://localhost:8080 --username testuser

    # Run inside a container
    ./setup-gerrit-user.py --container gerrit-local-test \\
        --username testuser --ssh-key "ssh-ed25519 AAAA..."

    # Loop over all instances from instances.json (replaces add-ssh-auth-keys.sh)
    SSH_AUTH_KEYS="ssh-ed25519 AAAA... user@host" \\
    ./setup-gerrit-user.py --instances-file /tmp/gerrit-action/instances.json \\
        --loop-instances --username testuser

Environment Variables:
    SSH_AUTH_KEYS       - SSH public keys (newline-separated)
    SSH_AUTH_USERNAME   - Username to create (alternative to --username)
    GERRIT_URL          - Gerrit base URL (alternative to --url)
    WORK_DIR            - Working directory containing instances.json
    USE_API_PATH        - Whether to use API path prefix (optional)
    DEBUG               - Enable debug logging if "true"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add lib directory to path for local imports
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

try:
    from gerrit_user_cli import build_parser
    from gerrit_user_inputs import collect_ssh_keys, validate_username
    from gerrit_user_instances import load_instances_file, run_loop_instances
    from gerrit_user_setup import get_container_gerrit_url, run_in_container, run_local
    from gerrit_user_summary import output_github_summary

    from gerrit_api import GerritAPIError
except ImportError as e:
    print(f"Error: Failed to import gerrit_api module: {e}", file=sys.stderr)
    print("Make sure 'requests' is installed: pip install requests", file=sys.stderr)
    sys.exit(1)

# Configure logging
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """Configure logging based on verbosity settings."""
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_multi_instance(args: argparse.Namespace, ssh_keys: list[str]) -> int:
    """Configure SSH keys on every instance in the instances file.

    Replaces add-ssh-auth-keys.sh.

    Args:
        args: Parsed command-line arguments.
        ssh_keys: Collected SSH public keys.

    Returns:
        Process exit code.
    """
    # Determine the instances file path
    instances_file = args.instances_file
    if not instances_file:
        # Fall back to $WORK_DIR/instances.json
        work_dir = os.environ.get("WORK_DIR", "/tmp")
        instances_file = os.path.join(work_dir, "instances.json")

    if not ssh_keys:
        logger.info("No SSH auth keys provided, skipping...")
        return 0

    instances = load_instances_file(instances_file)
    return run_loop_instances(
        instances,
        args.username,
        ssh_keys,
        name=args.name,
        email=args.email,
        add_to_admins=not args.no_admin,
        use_api_path=args.use_api_path,
        output_json=args.json,
    )


def run_single_instance(args: argparse.Namespace, ssh_keys: list[str]) -> int:
    """Configure SSH keys on the single Gerrit endpoint given on the CLI.

    Args:
        args: Parsed command-line arguments.
        ssh_keys: Collected SSH public keys.

    Returns:
        Process exit code.
    """
    if not ssh_keys:
        logger.warning("No SSH keys provided")

    logger.info(f"Setting up user: {args.username}")
    logger.info(f"SSH keys to add: {len(ssh_keys)}")

    try:
        if args.container:
            url = get_container_gerrit_url(args.container, args.port)
            account = run_in_container(
                container=args.container,
                url=url,
                username=args.username,
                ssh_keys=ssh_keys,
                name=args.name,
                email=args.email,
                add_to_admins=not args.no_admin,
            )
        else:
            account = run_local(
                url=args.url,
                username=args.username,
                ssh_keys=ssh_keys,
                name=args.name,
                email=args.email,
                add_to_admins=not args.no_admin,
            )

        # Output result
        if args.json:
            print(json.dumps(account, indent=2))
        else:
            print(f"✅ User '{args.username}' configured successfully")
            print(f"   Account ID: {account.get('_account_id', 'unknown')}")
            if account.get("email"):
                print(f"   Email: {account['email']}")
            if ssh_keys:
                print(f"   SSH keys: {len(ssh_keys)} added")

        # Write GitHub summary
        output_github_summary(account, args.username)

        return 0

    except GerritAPIError as e:
        logger.error(f"Gerrit API error: {e}")
        if e.response_text:
            logger.debug(f"Response: {e.response_text}")
        return 1
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


def main() -> int:
    """Main entry point."""
    parser = build_parser(epilog=__doc__)
    args = parser.parse_args()

    # Check for DEBUG environment variable
    if os.environ.get("DEBUG", "").lower() == "true":
        args.debug = True

    setup_logging(verbose=args.verbose, debug=args.debug)

    # Validate username
    username_error = validate_username(args.username)
    if username_error:
        logger.error(username_error)
        print(f"::error::{username_error}", file=sys.stderr)
        return 1

    # Collect SSH keys
    ssh_keys = collect_ssh_keys(args.ssh_keys, args.ssh_key_files)

    if args.loop_instances:
        return run_multi_instance(args, ssh_keys)
    return run_single_instance(args, ssh_keys)


if __name__ == "__main__":
    sys.exit(main())
