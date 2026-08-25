# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Instance metadata loading and per-instance SSH key configuration.

Split out of ``setup-gerrit-user.py``: this module owns everything the
multi-instance loop does for one entry of ``instances.json`` — reading
the file, deriving the Gerrit URL from the instance metadata, and
retrying the account setup while the container's auth subsystem is
still coming up.  Every name here is re-exported from
``setup-gerrit-user.py``.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from requests.exceptions import RequestException
from setup_user_client import run_local
from setup_user_model import (
    _INITIAL_RETRY_DELAY,
    _MAX_ATTEMPTS,
    _TRANSIENT_STATUS,
    CONFIGURED,
    FAILED,
    SKIPPED_NO_CONTAINER,
    InstanceOutcome,
)

from gerrit_api import GerritAPIError

logger = logging.getLogger(__name__)


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


def build_instance_url(instance: dict[str, Any], *, use_api_path: bool = False) -> str:
    """Build the Gerrit URL for one instance and log the parts it came from.

    Args:
        instance: Instance metadata dict (from ``instances.json``).
        use_api_path: Whether to use the ``api_path`` prefix.

    Returns:
        The base Gerrit URL to talk to this instance.
    """
    http_port = instance.get("http_port", 8080)
    logger.info(f"  HTTP Port: {http_port}")

    # Compute effective API path (must match logic in other scripts)
    api_path = instance.get("api_path", "")
    effective_api_path = ""
    if use_api_path and api_path:
        effective_api_path = api_path

    gerrit_url = f"http://localhost:{http_port}{effective_api_path}"
    logger.info(f"  Gerrit URL: {gerrit_url}")
    return gerrit_url


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
    setup_fn: Callable[..., dict[str, Any]] | None = None,
) -> InstanceOutcome:
    """Configure the user and SSH keys on a single instance.

    Args:
        slug: Instance slug.
        instance: Instance metadata dict (from ``instances.json``).
        username: Username to create/update.
        ssh_keys: List of SSH public key strings.
        name: Full name for the account.
        email: Email address for the account.
        add_to_admins: Whether to add to Administrators group.
        use_api_path: Whether to use API path prefix.
        output_json: Whether to output account info as JSON.
        setup_fn: Account-setup callable, defaulting to
            :func:`setup_user_client.run_local`.  The entry point passes
            its own reference so that rebinding ``run_local`` there is
            observed here, as it was when both lived in one module.

    Returns:
        The outcome for this instance.
    """
    logger.info("")
    logger.info(f"Processing instance: {slug}")
    logger.info("========================================")

    cid = instance.get("cid")
    if not cid or cid == "null":
        logger.warning(f"No container ID found for {slug}, skipping...")
        return SKIPPED_NO_CONTAINER

    logger.info(f"  Container ID: {cid[:12]}")

    gerrit_url = build_instance_url(instance, use_api_path=use_api_path)

    succeeded = _setup_with_retries(
        gerrit_url,
        slug,
        username,
        ssh_keys,
        name=name,
        email=email,
        add_to_admins=add_to_admins,
        output_json=output_json,
        setup_fn=setup_fn,
    )
    return CONFIGURED if succeeded else FAILED


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
    setup_fn: Callable[..., dict[str, Any]] | None = None,
) -> bool:
    """Run the setup against one URL, retrying transient failures.

    The Gerrit container may still be initialising its auth subsystem
    even though the health-check (which hits a public endpoint) already
    passed.

    Two kinds of failure are transient here.  Gerrit answering with a
    retryable status raises :class:`GerritAPIError`, which is matched
    against ``_TRANSIENT_STATUS``.  A container that is still starting
    up instead refuses the connection, or answers 5xx until urllib3
    exhausts the transport-level retry policy ``GerritTransport``
    mounts — both surface as a ``requests`` transport exception with
    no status code attached.  Treating those as transient is what puts
    container startup, the case this backoff exists for, back inside
    it.

    Returns:
        *True* if the account was configured, *False* otherwise.
    """
    setup = run_local if setup_fn is None else setup_fn
    retry_delay = _INITIAL_RETRY_DELAY

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            account = setup(
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

            return True

        except GerritAPIError as e:
            is_transient = e.status_code is None or e.status_code in _TRANSIENT_STATUS
            if is_transient and attempt < _MAX_ATTEMPTS:
                _log_retry(attempt, username, slug, e, retry_delay)
                time.sleep(retry_delay)
                # Increase delay for next attempt
                retry_delay *= 2
            else:
                _log_api_failure(username, slug, gerrit_url, attempt, e)
                return False

        except RequestException as e:
            # Connection refused, DNS failure, or a 5xx the transport
            # already retried and gave up on.  Always transient: none
            # of them mean the request was understood and rejected.
            if attempt < _MAX_ATTEMPTS:
                _log_retry(attempt, username, slug, e, retry_delay)
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.warning(
                    "Failed to configure SSH keys for %s on %s after %d attempt(s): %s",
                    username,
                    slug,
                    attempt,
                    e,
                )
                logger.warning("  Gerrit URL was: %s", gerrit_url)
                return False

        except Exception as e:
            logger.warning(
                "Unexpected error configuring SSH keys for %s on %s: %s",
                username,
                slug,
                e,
            )
            return False

    return False


def _log_retry(
    attempt: int,
    username: str,
    slug: str,
    error: Exception,
    retry_delay: int,
) -> None:
    """Report one failed attempt that will be retried."""
    logger.warning(
        "Attempt %d/%d failed for %s on %s: %s (retrying in %ds…)",
        attempt,
        _MAX_ATTEMPTS,
        username,
        slug,
        error,
        retry_delay,
    )


def _log_api_failure(
    username: str,
    slug: str,
    gerrit_url: str,
    attempt: int,
    error: GerritAPIError,
) -> None:
    """Report a Gerrit API failure that will not be retried."""
    logger.warning(
        "Failed to configure SSH keys for %s on %s after %d attempt(s): %s",
        username,
        slug,
        attempt,
        error,
    )
    if error.status_code is not None:
        logger.warning("  HTTP status: %s", error.status_code)
    if error.response_text:
        logger.debug("  Response body: %s", error.response_text)
    logger.warning("  Gerrit URL was: %s", gerrit_url)
