# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""SSH public key handling for Gerrit accounts.

Owns both halves of SSH key management:

* Client-side validation and parsing of ``authorized_keys``-style input
  (:func:`validate_ssh_key`, :func:`parse_ssh_keys`).
* The ``accounts/{id}/sshkeys`` endpoints, including the fresh-connection
  retry that works around Gerrit occasionally seeing a corrupted request
  line on a reused keepalive connection.

That retry policy is the reason this is a module of its own rather than
part of :mod:`gerrit_api_accounts`: it is the only endpoint group with
non-trivial failure handling.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, cast

from gerrit_api_accounts import GerritAccountsClient
from gerrit_api_protocol import (
    GerritAPIError,
    GerritConflictError,
    _looks_like_method_mangle,
)

logger = logging.getLogger(__name__)


class GerritSshKeyClient(GerritAccountsClient):
    """Adds SSH public key endpoints to the accounts client."""

    def list_ssh_keys(self, account: str | int = "self") -> list[dict[str, Any]]:
        """
        List SSH keys for an account.

        Args:
            account: Account identifier (default: "self")

        Returns:
            List of SSH key info dicts
        """
        return cast(list[dict[str, Any]], self.get(f"accounts/{account}/sshkeys"))

    def add_ssh_key(self, account: str | int, ssh_key: str) -> dict[str, Any]:
        """
        Add an SSH public key to an account.

        Args:
            account: Account identifier (ID, username, or "self")
            ssh_key: SSH public key in OpenSSH format

        Returns:
            Added SSH key info

        Raises:
            GerritAPIError: If the key is invalid
        """
        return cast(
            dict[str, Any],
            self.post(
                f"accounts/{account}/sshkeys",
                data=ssh_key,
                content_type="text/plain",
            ),
        )

    def add_ssh_keys(
        self, account: str | int, ssh_keys: list[str]
    ) -> list[dict[str, Any]]:
        """
        Add multiple SSH keys to an account.

        Each key is POSTed as ``text/plain`` to ``accounts/{id}/sshkeys``.
        We have observed an intermittent benign failure where Gerrit
        rejects one of several back-to-back POSTs on the same TCP
        connection with a ``"Not implemented: <garbled>POST <uri>"``
        response (the verb seen by Gerrit picks up 1-2 stray bytes,
        e.g. ``"alPOST"``).  This appears to be an HTTP keepalive /
        request-line corruption interaction in our path through
        Tailscale + the runner's request stack rather than a real
        method mismatch — keys 1 and 2 succeed, key 3 trips the
        garbled verb, and a separate fresh connection retries fine.

        We therefore:

        * Detect the ``"Not implemented:"`` body and retry once on
          a fresh ``Session`` (which forces a new TCP connection
          and bypasses any keepalive corruption).
        * If the retry succeeds, we don't pollute the log with a
          warning — the user-visible outcome is correct.
        * If it still fails, we emit a single ``INFO`` summary
          rather than a per-key WARNING, since the key add is
          best-effort and downstream auth still works via the keys
          that did land.

        Args:
            account: Account identifier
            ssh_keys: List of SSH public keys

        Returns:
            List of added SSH key infos
        """
        results: list[dict[str, Any]] = []
        deferred_failures: list[str] = []
        # ``ssh_keys`` may include blank lines and ``#`` comments;
        # the loop skips those without attempting any POST.  Tracking
        # the number of keys we actually tried makes the summary log
        # below match reality ("1 of 2 attempted failed" instead of
        # the misleading "1 of 5 keys failed" when 3 of the 5 entries
        # were comment/whitespace lines).
        attempted = 0

        for key in ssh_keys:
            key = key.strip()
            if not key or key.startswith("#"):
                continue
            attempted += 1
            try:
                result = self.add_ssh_key(account, key)
                results.append(result)
                logger.debug(f"Added SSH key {result.get('seq', '?')} to {account}")
                continue
            except GerritConflictError:
                logger.debug(f"SSH key already exists for {account}")
                continue
            except GerritAPIError as exc:
                if not _looks_like_method_mangle(exc):
                    # Genuine API error (validation, auth, etc.) —
                    # surface it as before.
                    logger.warning(f"Failed to add SSH key to {account}: {exc}")
                    continue

            # Fall-through: method-mangle path.  Retry once on a
            # fresh connection by closing and reopening the
            # underlying session adapters.  The reused requests
            # Session keeps cookies and XSRF state, so we only
            # need to clear the connection pool.
            # Closing a session is a best-effort hint; if it fails
            # the retry below will still go through whatever pool
            # requests rebuilds.
            with contextlib.suppress(Exception):
                self.session.close()

            try:
                result = self.add_ssh_key(account, key)
                results.append(result)
                logger.debug(
                    "Added SSH key %s to %s after fresh-connection retry",
                    result.get("seq", "?"),
                    account,
                )
            except GerritConflictError:
                logger.debug(f"SSH key already exists for {account} (after retry)")
            except GerritAPIError as exc:
                # Retry also failed — record but do not WARN per key.
                deferred_failures.append(str(exc))

        if deferred_failures:
            # Emit a single concise INFO line summarising any keys
            # that could not be added even after retry.  Downstream
            # auth still works for any keys that did land, and the
            # admin-group setup proceeds regardless.  The total uses
            # ``attempted`` rather than ``len(ssh_keys)`` so blank/
            # comment-only entries do not inflate the denominator.
            logger.info(
                "%d of %d SSH key(s) for %s could not be added "
                "(non-fatal); proceeding without them",
                len(deferred_failures),
                attempted,
                account,
            )
            # Surface the individual retry-path errors at DEBUG so they
            # are diagnosable when troubleshooting, without inflating
            # the concise INFO summary above.
            for idx, failure in enumerate(deferred_failures, start=1):
                logger.debug(
                    "  SSH key failure %d/%d for %s: %s",
                    idx,
                    len(deferred_failures),
                    account,
                    failure,
                )

        return results

    def delete_ssh_key(self, account: str | int, key_seq: int) -> None:
        """Delete an SSH key by sequence number."""
        self.delete(f"accounts/{account}/sshkeys/{key_seq}")


def validate_ssh_key(key: str) -> bool:
    """
    Validate SSH public key format.

    Args:
        key: SSH public key string

    Returns:
        True if the key appears to be valid
    """
    key = key.strip()
    if not key or key.startswith("#"):
        return True  # Empty or comment is OK (will be skipped)

    # Valid SSH key types
    valid_types = (
        "ssh-rsa",
        "ssh-ed25519",
        "ssh-dss",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "sk-ssh-ed25519",
        "sk-ecdsa-sha2-nistp256",
    )

    parts = key.split()
    if len(parts) < 2:
        return False

    return parts[0] in valid_types


def parse_ssh_keys(keys_string: str) -> list[str]:
    """
    Parse a string containing one or more SSH keys.

    Args:
        keys_string: Newline-separated SSH public keys

    Returns:
        List of individual SSH key strings
    """
    keys = []
    for line in keys_string.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            if validate_ssh_key(line):
                keys.append(line)
            else:
                logger.warning(f"Invalid SSH key format: {line[:50]}...")
    return keys
