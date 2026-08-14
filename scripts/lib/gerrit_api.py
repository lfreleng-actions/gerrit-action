#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Gerrit REST API client for DEVELOPMENT_BECOME_ANY_ACCOUNT mode.

This module provides a clean Python interface for interacting with Gerrit's
REST API when running in DEVELOPMENT_BECOME_ANY_ACCOUNT mode. It handles:

- Cookie-based session authentication (becoming any account)
- XSRF token management for write operations
- JSON response parsing (stripping Gerrit's magic prefix)
- Account and SSH key management

The implementation is layered, each module extending the one before it,
so that the public client below only has to express the high-level
workflows:

``gerrit_api_protocol``   wire format, error taxonomy, response parsing
``gerrit_api_transport``  session, retries, URLs, HTTP verbs
``gerrit_api_session``    XSRF/cookie lifecycle and verification
``gerrit_api_accounts``   account, group and cache endpoints
``gerrit_api_ssh_keys``   SSH key endpoints, validation and parsing
``gerrit_api_auth``       login and bootstrap strategies
``gerrit_api``            :class:`GerritDevClient` and the CLI entry point

Names from those modules are re-exported here, so importing from
``gerrit_api`` remains the supported way to use the client.

Usage:
    from gerrit_api import GerritDevClient

    client = GerritDevClient("http://localhost:8080")
    client.become_account(1000000)

    # Create a user
    account = client.create_account("testuser", name="Test User")

    # Add SSH keys
    client.add_ssh_key(account["_account_id"], "ssh-ed25519 AAAA... user@host")

    # Add to Administrators group
    client.add_to_group(account["_account_id"], "Administrators")
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from gerrit_api_auth import GerritAuthClient
from gerrit_api_protocol import (
    DEFAULT_ADMIN_ACCOUNTS,
    DEFAULT_TIMEOUT,
    GERRIT_MAGIC_JSON_PREFIX,
    GerritAPIError,
    GerritAuthError,
    GerritConflictError,
    GerritNotFoundError,
    _cookie_names_from_header,
    _looks_like_method_mangle,
    _parse_response,
    _strip_gerrit_prefix,
)
from gerrit_api_ssh_keys import parse_ssh_keys, validate_ssh_key

# Configure logging
logger = logging.getLogger(__name__)

# Names re-exported for the callers and tests that import them from
# ``gerrit_api``.  The underscore-prefixed entries are listed
# deliberately: they are internal by convention but form part of this
# module's established import surface and must keep resolving as
# ``gerrit_api.<name>``.
__all__ = [
    "DEFAULT_ADMIN_ACCOUNTS",
    "DEFAULT_TIMEOUT",
    "GERRIT_MAGIC_JSON_PREFIX",
    "GerritAPIError",
    "GerritAuthError",
    "GerritConflictError",
    "GerritDevClient",
    "GerritNotFoundError",
    "_cookie_names_from_header",
    "_looks_like_method_mangle",
    "_parse_response",
    "_strip_gerrit_prefix",
    "main",
    "parse_ssh_keys",
    "validate_ssh_key",
]


class GerritDevClient(GerritAuthClient):
    """
    Gerrit REST API client for DEVELOPMENT_BECOME_ANY_ACCOUNT mode.

    This client handles cookie-based session authentication by "becoming"
    a specified account, and properly manages XSRF tokens for write operations.

    Args:
        base_url: Base URL of the Gerrit server (e.g., "http://localhost:8080")
        verify_ssl: Whether to verify SSL certificates (default: True)
        timeout: Request timeout in seconds (default: 30)

    Example:
        >>> client = GerritDevClient("http://localhost:8080")
        >>> client.become_account(1000000)
        >>> client.get_account("self")
        {'_account_id': 1000000, 'name': 'Administrator', ...}
    """

    # =========================================================================
    # High-level Operations
    # =========================================================================

    def setup_user_with_ssh_keys(
        self,
        username: str,
        ssh_keys: list[str],
        name: str | None = None,
        email: str | None = None,
        add_to_admins: bool = True,
    ) -> dict[str, Any]:
        """
        Set up a user account with SSH keys.

        This is a high-level operation that:
        1. Creates the account if it doesn't exist
        2. Adds the provided SSH keys
        3. Optionally adds the user to Administrators group
           (with retry and post-add verification)
        4. Flushes relevant caches

        Args:
            username: Username to create/update
            ssh_keys: List of SSH public keys to add
            name: Full name (defaults to username)
            email: Email address (defaults to username@example.com)
            add_to_admins: Whether to add to Administrators group

        Returns:
            Account info dict

        Raises:
            GerritAPIError: If the user cannot be added to the
                Administrators group after retries and the
                add_to_admins flag is True.
        """
        logger.info(f"Setting up user: {username}")

        # Default values
        if not name:
            name = username
        if not email:
            email = f"{username}@example.com"

        # Get or create account
        account = self.get_or_create_account(username, name=name, email=email)
        account_id = account["_account_id"]
        logger.info(f"Account ID: {account_id}")

        # Add SSH keys
        if ssh_keys:
            # Filter empty lines and comments
            valid_keys = [
                k.strip()
                for k in ssh_keys
                if k.strip() and not k.strip().startswith("#")
            ]
            if valid_keys:
                added = self.add_ssh_keys(account_id, valid_keys)
                logger.info(f"Added {len(added)} SSH keys")

        # Add to Administrators group with retry and verification
        if add_to_admins:
            self._add_to_admins_with_retry(username, account_id)

        # Flush caches
        self.flush_cache()
        logger.info(f"User {username} configured successfully")

        return account

    def _add_to_admins_with_retry(
        self,
        username: str,
        account_id: int,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        """Add an account to the Administrators group with retry.

        Retries on transient failures (the Gerrit auth subsystem may
        lag behind the health check on container startup).  After a
        successful API call the membership is verified by listing the
        group members and confirming the account ID is present.

        Args:
            username: Username (for log messages).
            account_id: Gerrit account ID to add.
            max_attempts: Maximum number of add attempts.
            retry_delay: Initial delay between retries (doubles
                each attempt).

        Raises:
            GerritAPIError: If the account cannot be added or
                verified after all attempts.
        """
        group = "Administrators"
        last_error: GerritAPIError | None = None
        delay = retry_delay

        for attempt in range(1, max_attempts + 1):
            try:
                self.add_to_group(account_id, group)
                logger.info(
                    "Added %s to %s group (attempt %d/%d)",
                    username,
                    group,
                    attempt,
                    max_attempts,
                )
            except GerritConflictError:
                logger.debug(
                    "%s already in %s group",
                    username,
                    group,
                )
            except GerritAPIError as exc:
                last_error = exc
                if attempt < max_attempts:
                    logger.warning(
                        "Attempt %d/%d to add %s to %s failed: %s (retrying in %.0fs)",
                        attempt,
                        max_attempts,
                        username,
                        group,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                # Final attempt failed — fall through to raise
                break

            # --- Verify membership after a successful add -----------
            if self._verify_group_membership(account_id, group):
                logger.info(
                    "Verified %s is in %s group ✅",
                    username,
                    group,
                )
                return

            # Verification failed — treat as transient and retry
            last_error = GerritAPIError(
                f"Verification failed: {username} (account {account_id}) "
                f"not found in {group} members after add_to_group "
                f"reported success"
            )
            if attempt < max_attempts:
                logger.warning(
                    "Attempt %d/%d: %s not found in %s members "
                    "after add (retrying in %.0fs)",
                    attempt,
                    max_attempts,
                    username,
                    group,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
                continue

        # All attempts exhausted
        raise GerritAPIError(
            f"Failed to add {username} to {group} after "
            f"{max_attempts} attempt(s): {last_error}"
        )

    def _verify_group_membership(
        self,
        account_id: int,
        group: str,
    ) -> bool:
        """Check whether an account is a member of a group.

        Args:
            account_id: Gerrit account ID to look for.
            group: Group name to inspect.

        Returns:
            True if the account is present in the group's member
            list, False otherwise (including on API errors).
        """
        try:
            members = self.list_group_members(group)
            return any(m.get("_account_id") == account_id for m in members)
        except GerritAPIError as exc:
            logger.warning(
                "Could not verify %s membership in %s: %s",
                account_id,
                group,
                exc,
            )
            return False


# =============================================================================
# CLI Interface
# =============================================================================


def main() -> int:
    """CLI entry point for testing."""
    from gerrit_api_cli import build_parser, configure_logging, dispatch

    args = build_parser().parse_args()
    configure_logging(args.verbose)

    try:
        client = GerritDevClient(args.url)
        client.become_account(args.account_id)
        dispatch(client, args)
        return 0

    except GerritAPIError as e:
        logger.error(f"API error: {e}")
        if e.response_text:
            logger.error(f"Response: {e.response_text}")
        return 1
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
