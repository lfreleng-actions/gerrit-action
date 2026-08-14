# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Account, group and cache endpoints of the Gerrit REST API.

Owns the thin, single-request wrappers around the identity-related parts
of Gerrit's API: creating and looking up accounts, inspecting and
mutating group membership, and flushing the server-side caches that
those mutations invalidate.

Everything here is a direct endpoint mapping with no retry or
verification policy of its own; the orchestration that combines these
calls lives in :mod:`gerrit_api`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, cast

from gerrit_api_protocol import (
    GerritAPIError,
    GerritConflictError,
    GerritNotFoundError,
    _parse_response,
)
from gerrit_api_session import GerritSessionClient

logger = logging.getLogger(__name__)


class GerritAccountsClient(GerritSessionClient):
    """Adds account, group and cache endpoints to the session client."""

    # =========================================================================
    # Account Management
    # =========================================================================

    def get_account(self, account: str | int) -> dict[str, Any]:
        """
        Get account details.

        Args:
            account: Account identifier (ID, username, email, or "self")

        Returns:
            Account info dict with _account_id, name, email, username, etc.
        """
        return cast(dict[str, Any], self.get(f"accounts/{account}"))

    def account_exists(self, account: str | int) -> bool:
        """Check if an account exists."""
        try:
            self.get_account(account)
            return True
        except GerritNotFoundError:
            return False

    def create_account(
        self,
        username: str,
        name: str | None = None,
        email: str | None = None,
        ssh_key: str | None = None,
        groups: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create a new account.

        Args:
            username: Username for the new account
            name: Full name (optional, defaults to username)
            email: Email address (optional)
            ssh_key: SSH public key to add (optional)
            groups: List of group names to add the account to (optional)

        Returns:
            Created account info

        Raises:
            GerritConflictError: If account already exists
        """
        payload: dict[str, Any] = {"username": username}
        if name:
            payload["name"] = name
        if email:
            payload["email"] = email
        if ssh_key:
            payload["ssh_key"] = ssh_key
        if groups:
            payload["groups"] = groups

        return cast(dict[str, Any], self.put(f"accounts/{username}", data=payload))

    def get_or_create_account(
        self,
        username: str,
        name: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """
        Get an existing account or create it if it doesn't exist.

        Args:
            username: Username to look up or create
            name: Full name for new account
            email: Email for new account

        Returns:
            Account info dict
        """
        try:
            return self.get_account(username)
        except GerritNotFoundError:
            try:
                return self.create_account(username, name=name, email=email)
            except GerritConflictError:
                # Race condition - account was created between check and create
                return self.get_account(username)

    def set_account_name(self, account: str | int, name: str) -> str:
        """Set the full name for an account."""
        return cast(str, self.put(f"accounts/{account}/name", data={"name": name}))

    # =========================================================================
    # Group Management
    # =========================================================================

    def get_group(self, group: str) -> dict[str, Any]:
        """Get group details."""
        return cast(dict[str, Any], self.get(f"groups/{group}"))

    def list_group_members(self, group: str) -> list[dict[str, Any]]:
        """List members of a group."""
        return cast(list[dict[str, Any]], self.get(f"groups/{group}/members"))

    def add_to_group(self, account: str | int, group: str) -> dict[str, Any] | None:
        """
        Add an account to a group.

        Args:
            account: Account identifier
            group: Group name (e.g., "Administrators")

        Returns:
            Account info of the added member, or None if response isn't JSON
        """
        url = self._make_url(f"groups/{group}/members/{account}")
        headers = self._get_headers()

        response = self.session.put(
            url,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )

        # Group membership PUT may return non-JSON response on success
        result = _parse_response(response, allow_non_json=True)
        if isinstance(result, dict):
            return cast(dict[str, Any], result)
        return None

    def remove_from_group(self, account: str | int, group: str) -> None:
        """Remove an account from a group."""
        self.delete(f"groups/{group}/members/{account}")

    # =========================================================================
    # Cache Management
    # =========================================================================

    def flush_cache(self, cache_name: str | None = None) -> None:
        """
        Flush Gerrit caches.

        Args:
            cache_name: Specific cache to flush, or None to flush important caches
        """
        if cache_name:
            with contextlib.suppress(GerritAPIError):
                self.post(f"config/server/caches/{cache_name}/flush")
        else:
            # Flush caches important for account management
            # Note: Cache names vary by Gerrit version
            for cache in [
                "accounts",
                "groups",
                "sshkeys",
                "ldap_groups",
            ]:
                try:
                    self.post(f"config/server/caches/{cache}/flush")
                    logger.debug(f"Flushed cache: {cache}")
                except GerritAPIError as exc:
                    # Cache may not exist or flush may not be supported.
                    logger.debug("Cache '%s' flush skipped: %s", cache, exc)
