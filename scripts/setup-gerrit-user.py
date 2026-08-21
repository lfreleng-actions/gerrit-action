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

Implementation:
    Each step lives in a focused module under scripts/lib and is
    re-exported here, so this file stays the single entry point:
    setup_user_model (username rules, retry policy, outcome record),
    setup_user_cli (argument parser), setup_user_input (logging, key
    collection, validation), setup_user_client (account setup against
    one Gerrit URL), setup_user_instances (instances.json loading and
    per-instance retries) and setup_user_summary (closing log banner
    and GitHub step summaries).

    The run sequences stay here so that main() and run_loop_instances()
    resolve every step as an attribute of this module, which is how
    callers substitute individual steps.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Add lib directory to path for local imports
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

# ``gerrit_api`` is imported first, and guarded, so a missing
# ``requests`` reports the actionable message below rather than a
# traceback from whichever setup_user_* module got there first.
try:
    from gerrit_api import (
        GerritAPIError,
        GerritDevClient,  # noqa: F401
        parse_ssh_keys,  # noqa: F401
    )
except ImportError as e:
    print(f"Error: Failed to import gerrit_api module: {e}", file=sys.stderr)
    print("Make sure 'requests' is installed: pip install requests", file=sys.stderr)
    sys.exit(1)

from setup_user_cli import build_parser  # noqa: E402
from setup_user_client import (  # noqa: E402
    get_container_gerrit_url,
    run_in_container,
    run_local,
)
from setup_user_input import (  # noqa: E402
    collect_ssh_keys,
    read_ssh_keys,  # noqa: F401
    setup_logging,
    validate_username,
)
from setup_user_instances import (  # noqa: E402
    build_instance_url,  # noqa: F401
    configure_instance,
    load_instances_file,
)
from setup_user_model import (  # noqa: E402
    _USERNAME_MAX_LEN,  # noqa: F401
    _USERNAME_RE,  # noqa: F401
    InstanceOutcome,  # noqa: F401
)
from setup_user_summary import (  # noqa: E402
    log_loop_completion,
    output_github_summary,
    output_multi_instance_summary,
)

# Configure logging
logger = logging.getLogger(__name__)

__all__ = [
    "InstanceOutcome",
    # The underscore-prefixed entries are long-standing internals that
    # callers import from this module by name; they are listed so the
    # re-export stays explicit.
    "_USERNAME_MAX_LEN",
    "_USERNAME_RE",
    "build_instance_url",
    "build_parser",
    "collect_ssh_keys",
    "configure_instance",
    "get_container_gerrit_url",
    "load_instances_file",
    "log_loop_completion",
    "main",
    "output_github_summary",
    "output_multi_instance_summary",
    "read_ssh_keys",
    "run_in_container",
    "run_local",
    "run_loop_instances",
    "setup_logging",
    "validate_username",
]


def run_loop_instances(
    instances: dict[str, Any],
    username: str,
    ssh_keys: list[str],
    *,
    name: str | None = None,
    email: str | None = None,
    add_to_admins: bool = True,
    use_api_path: bool = False,
    output_json: bool = False,
) -> int:
    """Run user setup across all instances from instances.json.

    This replaces the main loop from add-ssh-auth-keys.sh.

    Args:
        instances: Mapping of slug to instance metadata dict.
        username: Username to create/update.
        ssh_keys: List of SSH public key strings.
        name: Full name for the account.
        email: Email address for the account.
        add_to_admins: Whether to add to Administrators group.
        use_api_path: Whether to use API path prefix.
        output_json: Whether to output account info as JSON.

    Returns:
        Exit code: 0 if all instances succeeded, 1 if any failed.
    """
    if not ssh_keys:
        logger.info("No SSH auth keys provided, skipping...")
        return 0

    logger.info("Adding SSH authentication keys to Gerrit container(s)...")
    logger.info(f"Will create/update Gerrit user: {username}")

    success_count = 0
    failure_count = 0
    summary_rows: list[tuple[str, str]] = []

    for slug in sorted(instances.keys()):
        outcome = configure_instance(
            slug,
            instances[slug],
            username,
            ssh_keys,
            name=name,
            email=email,
            add_to_admins=add_to_admins,
            use_api_path=use_api_path,
            output_json=output_json,
            setup_fn=run_local,
        )
        summary_rows.append((slug, outcome.status))
        if outcome.kind == "configured":
            success_count += 1
        elif outcome.kind == "failed":
            failure_count += 1

    log_loop_completion(username, success_count, failure_count)

    # Write multi-instance GitHub summary
    output_multi_instance_summary(username, summary_rows, failure_count)

    return 1 if failure_count > 0 else 0


def _run_multi_instance(args: argparse.Namespace, ssh_keys: list[str]) -> int:
    """Configure every instance in the instances file.

    This is the mode that replaces add-ssh-auth-keys.sh.
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


def _run_single_instance(args: argparse.Namespace, ssh_keys: list[str]) -> int:
    """Configure one Gerrit endpoint (the original behaviour)."""
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
        return _run_multi_instance(args, ssh_keys)

    return _run_single_instance(args, ssh_keys)


if __name__ == "__main__":
    sys.exit(main())
