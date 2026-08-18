# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Multi-instance SSH key configuration driven from ``instances.json``.

This module is the Python replacement for ``add-ssh-auth-keys.sh``: it
walks every Gerrit container recorded in the instances metadata file
and configures the same account on each one, collecting a per-instance
outcome so a single unreachable container does not abort the run.

Each instance is retried independently.  Gerrit's authentication
subsystem frequently is not ready at the moment the container passes
its health check, so a short exponential back-off is applied to
transient failures before an instance is declared failed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

from gerrit_user_setup import run_local
from gerrit_user_summary import log_loop_summary, output_multi_instance_summary

from gerrit_api import GerritAPIError

logger = logging.getLogger(__name__)

# HTTP status codes worth retrying. 401/403 are included because the
# Gerrit auth subsystem may not be fully ready immediately after the
# container passes its health check (which hits a public endpoint).
_TRANSIENT_STATUS_CODES = {401, 403, 429, 500, 502, 503, 504}


class InstanceOutcome(Enum):
    """Result of configuring one instance, and its summary-table text."""

    CONFIGURED = "✅ Configured"
    SKIPPED = "⚠️ Skipped (no container)"
    FAILED = "❌ Failed"


def load_instances_file(path: str) -> dict[str, Any]:
    """Load instances metadata from a JSON file.

    Returns:
        Dictionary mapping slug to instance metadata.

    Raises:
        SystemExit: If the file does not exist or contains invalid JSON.
    """
    instances_path = Path(path)
    if not instances_path.exists():
        logger.error(f"Instances file not found: {path}")
        sys.exit(1)

    try:
        data = json.loads(instances_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.error(f"Instances file must contain a JSON object: {path}")
            sys.exit(1)
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in instances file {path}: {e}")
        sys.exit(1)


def _setup_with_retries(
    gerrit_url: str,
    slug: str,
    username: str,
    ssh_keys: list[str],
    *,
    name: str | None = None,
    email: str | None = None,
    add_to_admins: bool = True,
    output_json: bool = False,
) -> bool:
    """Configure one Gerrit endpoint, retrying transient failures.

    The Gerrit container may still be initialising its auth subsystem
    even though the health-check (which hits a public endpoint)
    already passed, so transient errors are retried with a doubling
    delay before the instance is given up on.

    Args:
        gerrit_url: Fully-qualified Gerrit base URL for this instance.
        slug: Instance slug, for log messages.
        username: Username to create/update.
        ssh_keys: List of SSH public key strings.
        name: Full name for the account.
        email: Email address for the account.
        add_to_admins: Whether to add to Administrators group.
        output_json: Whether to print account info as JSON.

    Returns:
        *True* if the account was configured, *False* otherwise.
    """
    max_attempts = 3
    retry_delay = 3  # seconds
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            account = run_local(
                url=gerrit_url,
                username=username,
                ssh_keys=ssh_keys,
                name=name,
                email=email,
                add_to_admins=add_to_admins,
            )

            if output_json:
                print(json.dumps(account, indent=2))
            else:
                logger.info(f"  SSH keys configured for {username} ✅")

            last_error = None
            break

        except GerritAPIError as e:
            last_error = e
            # Only retry on transient failures: network errors
            # (no status code) or specific HTTP codes.
            is_transient = (
                e.status_code is None or e.status_code in _TRANSIENT_STATUS_CODES
            )
            if is_transient and attempt < max_attempts:
                logger.warning(
                    "Attempt %d/%d failed for %s on %s: %s (retrying in %ds…)",
                    attempt,
                    max_attempts,
                    username,
                    slug,
                    e,
                    retry_delay,
                )
                time.sleep(retry_delay)
                # Increase delay for next attempt
                retry_delay *= 2
            else:
                logger.warning(
                    "Failed to configure SSH keys for %s on %s after %d attempt(s): %s",
                    username,
                    slug,
                    attempt,
                    e,
                )
                if e.status_code is not None:
                    logger.warning("  HTTP status: %s", e.status_code)
                if e.response_text:
                    logger.debug("  Response body: %s", e.response_text)
                logger.warning("  Gerrit URL was: %s", gerrit_url)
                break

        except Exception as e:
            last_error = e
            logger.warning(
                "Unexpected error configuring SSH keys for %s on %s: %s",
                username,
                slug,
                e,
            )
            break

    return last_error is None


def configure_instance(
    slug: str,
    instance: dict[str, Any],
    username: str,
    ssh_keys: list[str],
    *,
    name: str | None = None,
    email: str | None = None,
    add_to_admins: bool = True,
    use_api_path: bool = False,
    output_json: bool = False,
) -> InstanceOutcome:
    """Configure the account on a single instance from ``instances.json``.

    Args:
        slug: Instance slug.
        instance: Instance metadata dict.
        username: Username to create/update.
        ssh_keys: List of SSH public key strings.
        name: Full name for the account.
        email: Email address for the account.
        add_to_admins: Whether to add to Administrators group.
        use_api_path: Whether to use the instance's API path prefix.
        output_json: Whether to print account info as JSON.

    Returns:
        The outcome for this instance.
    """
    logger.info("")
    logger.info(f"Processing instance: {slug}")
    logger.info("========================================")

    # Get container ID
    cid = instance.get("cid")
    if not cid or cid == "null":
        logger.warning(f"No container ID found for {slug}, skipping...")
        return InstanceOutcome.SKIPPED

    logger.info(f"  Container ID: {cid[:12]}")

    # Get HTTP port
    http_port = instance.get("http_port", 8080)
    logger.info(f"  HTTP Port: {http_port}")

    # Compute effective API path (must match logic in other scripts)
    api_path = instance.get("api_path", "")
    effective_api_path = ""
    if use_api_path and api_path:
        effective_api_path = api_path

    # Build Gerrit URL
    gerrit_url = f"http://localhost:{http_port}{effective_api_path}"
    logger.info(f"  Gerrit URL: {gerrit_url}")

    configured = _setup_with_retries(
        gerrit_url,
        slug,
        username,
        ssh_keys,
        name=name,
        email=email,
        add_to_admins=add_to_admins,
        output_json=output_json,
    )
    return InstanceOutcome.CONFIGURED if configured else InstanceOutcome.FAILED


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
        )
        summary_rows.append((slug, outcome.value))
        if outcome is InstanceOutcome.CONFIGURED:
            success_count += 1
        elif outcome is InstanceOutcome.FAILED:
            failure_count += 1

    # Final summary
    log_loop_summary(username, success_count, failure_count)

    # Write multi-instance GitHub summary
    output_multi_instance_summary(username, summary_rows, failure_count)

    return 1 if failure_count > 0 else 0
