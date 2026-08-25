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
import time
from typing import Any, cast

from gerrit_api_accounts import GerritAccountsClient
from gerrit_api_protocol import (
    GerritAPIError,
    GerritConflictError,
    _looks_like_method_mangle,
)
from gerrit_api_transport import RETRYABLE_STATUSES
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

# The key POST is retried here rather than by the transport, so the
# attempt budget and backoff live here too.  ``Retry(total=3)`` allows
# three retries *after* the initial request, so four attempts matches
# the transport policy this replaces for the endpoint rather than
# quietly shrinking it.
_KEY_POST_ATTEMPTS = 4
_KEY_POST_BACKOFF = 0.5


def _ssh_key_identity(key: str) -> tuple[str, ...]:
    """Return the fields that identify an SSH public key.

    An OpenSSH public key is ``<algorithm> <base64 material> [comment]``.
    The comment is free-form and Gerrit echoes back whatever it stored,
    so only the first two fields are compared when deciding whether a
    key is already on an account.  Returns an empty tuple for input
    that is not shaped like a key, which callers treat as "no match".
    """
    fields = tuple(key.split()[:2])
    return fields if len(fields) == 2 else ()


def _match_ssh_key(keys: list[dict[str, Any]], ssh_key: str) -> dict[str, Any] | None:
    """Return the entry in *keys* holding the same material, or None."""
    identity = _ssh_key_identity(ssh_key)
    if not identity:
        return None

    for key in keys:
        material = key.get("ssh_public_key", "")
        if isinstance(material, str) and _ssh_key_identity(material) == identity:
            return key
    return None


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

        This is the only non-idempotent write in the client, so it is
        the only one that cannot ride the session's ``urllib3`` retry
        policy.  That policy includes ``POST`` and replays it on
        ``5xx`` *inside* the call, so a request Gerrit applied but
        whose response was lost would be repeated before any code here
        could check whether it landed — adding the key twice.

        The retry therefore moves up a level: the POST is issued with
        transport retries suspended, and each failed attempt is
        followed by a read-back of the account's keys.  If the key is
        there, the attempt succeeded despite the error and the existing
        entry is returned; if not, the POST is retried.  Every attempt
        is thus made idempotent by inspection, which keeps the
        resilience the surrounding code relies on (see
        :meth:`add_ssh_keys`) without the duplication risk.

        Args:
            account: Account identifier (ID, username, or "self")
            ssh_key: SSH public key in OpenSSH format

        Returns:
            Added SSH key info, or the existing entry when the key was
            already present on the account

        Raises:
            GerritAPIError: If the key is invalid
        """
        existing = self._find_ssh_key(account, ssh_key)
        if existing is not None:
            logger.debug(
                "SSH key %s already present on %s; skipping add",
                existing.get("seq", "?"),
                account,
            )
            return existing

        attempt = 0
        while True:
            attempt += 1
            try:
                with self.no_transport_retries():
                    return cast(
                        dict[str, Any],
                        self.post(
                            f"accounts/{account}/sshkeys",
                            data=ssh_key,
                            content_type="text/plain",
                        ),
                    )
            except GerritAPIError as exc:
                # Only the statuses the transport used to replay are
                # retried.  Anything else — 409 Conflict, a validation
                # rejection, the garbled-verb 501 that add_ssh_keys
                # handles on a fresh connection — is a real answer and
                # is raised for the caller to act on.
                if exc.status_code not in RETRYABLE_STATUSES:
                    raise
                last_error: Exception = exc
            except RequestException as exc:
                # Connection dropped or reset: the request may or may
                # not have reached Gerrit, which is exactly why the
                # read-back below has to decide.
                last_error = exc

            # Strict read-back.  Unlike the preflight, this one must
            # distinguish "key absent" from "could not look": reposting
            # on an inspection failure is exactly the duplicate this
            # method exists to avoid.  Only a successful key list that
            # does not contain the key justifies another POST.
            keys = self._read_ssh_keys(account)
            if keys is None:
                logger.debug(
                    "Cannot confirm whether the SSH key for %s landed "
                    "after %s; not reposting",
                    account,
                    last_error,
                )
                raise last_error

            landed = _match_ssh_key(keys, ssh_key)
            if landed is not None:
                logger.debug(
                    "SSH key %s for %s landed despite %s; not retrying",
                    landed.get("seq", "?"),
                    account,
                    last_error,
                )
                return landed

            if attempt >= _KEY_POST_ATTEMPTS:
                raise last_error

            logger.debug(
                "SSH key add attempt %d/%d for %s failed: %s",
                attempt,
                _KEY_POST_ATTEMPTS,
                account,
                last_error,
            )
            time.sleep(_KEY_POST_BACKOFF * attempt)

    def _read_ssh_keys(self, account: str | int) -> list[dict[str, Any]] | None:
        """Return the account's SSH keys, or None if they cannot be read.

        The ``None`` return is deliberately distinct from an empty
        list: callers deciding whether to repeat a non-idempotent write
        must not read "could not look" as "not there".

        A failure here already means four attempts have been made, as
        the GET is idempotent and still rides the session's transport
        retry policy.
        """
        try:
            return self.list_ssh_keys(account)
        except (GerritAPIError, RequestException) as exc:
            logger.debug("Could not list SSH keys for %s: %s", account, exc)
            return None

    def _find_ssh_key(self, account: str | int, ssh_key: str) -> dict[str, Any] | None:
        """Return the account's entry for *ssh_key*, or None.

        This is the best-effort preflight.  A failure to read the key
        list is not fatal: the caller falls through to the POST, which
        is exactly the behaviour before this guard existed.  Only a
        positive match suppresses the write, so both Gerrit errors and
        transport failures return None rather than propagating.

        The read-back *between* POST attempts uses
        :meth:`_read_ssh_keys` directly instead, because there an
        unreadable list must not be mistaken for an absent key.
        """
        keys = self._read_ssh_keys(account)
        if keys is None:
            return None
        return _match_ssh_key(keys, ssh_key)

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

        Every per-key failure is contained here rather than allowed to
        propagate, including the transport failures
        :meth:`add_ssh_key` raises when it cannot confirm whether an
        ambiguous POST landed.  The account-setup caller retries on
        transport errors, and a second pass through this method would
        re-run the best-effort preflight and could repost a key that
        had in fact landed.

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
            except RequestException as exc:
                # ``add_ssh_key`` raises this when it could not
                # establish whether an ambiguous POST landed.  It must
                # not escape: the caller retries the whole account
                # setup on transport errors, and a second pass would
                # run the best-effort preflight again and could repost
                # a key that did land — reintroducing the duplicate the
                # strict read-back exists to prevent.  Record it as a
                # deferred failure like any other per-key problem.
                deferred_failures.append(str(exc))
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
            except (GerritAPIError, RequestException) as exc:
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
