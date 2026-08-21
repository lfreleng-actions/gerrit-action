# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Account setup runs against a single Gerrit endpoint.

Split out of ``setup-gerrit-user.py``: this module owns the calls that
authenticate against one Gerrit instance and create or update the user
there.  Every name here is re-exported from ``setup-gerrit-user.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from gerrit_api import GerritDevClient

logger = logging.getLogger(__name__)


def get_container_gerrit_url(_container: str, port: int = 8080) -> str:
    """
    Get the Gerrit URL for a container.

    For containers, we use localhost with the mapped port.
    The container name is accepted for API consistency but not used
    since we connect via localhost with mapped ports.
    """
    return f"http://localhost:{port}"


def run_in_container(
    container: str,
    url: str,
    username: str,
    ssh_keys: list[str],
    name: str | None = None,
    email: str | None = None,
    add_to_admins: bool = True,
) -> dict[str, Any]:
    """
    Run the user setup directly (container is accessible via localhost).

    Args:
        container: Container name (for logging)
        url: Gerrit URL
        username: Username to create
        ssh_keys: List of SSH public keys
        name: Full name for the account
        email: Email address for the account
        add_to_admins: Whether to add to Administrators group

    Returns:
        Account info dict
    """
    logger.info(f"Setting up user in container: {container}")
    logger.info(f"Gerrit URL: {url}")

    client = GerritDevClient(url)
    admin_id = client.become_admin()
    logger.info(f"Authenticated as admin account {admin_id}")

    result: dict[str, Any] = client.setup_user_with_ssh_keys(
        username=username,
        ssh_keys=ssh_keys,
        name=name,
        email=email,
        add_to_admins=add_to_admins,
    )
    return result


def run_local(
    url: str,
    username: str,
    ssh_keys: list[str],
    name: str | None = None,
    email: str | None = None,
    add_to_admins: bool = True,
) -> dict[str, Any]:
    """
    Run the user setup against a local Gerrit instance.

    Args:
        url: Gerrit URL
        username: Username to create
        ssh_keys: List of SSH public keys
        name: Full name for the account
        email: Email address for the account
        add_to_admins: Whether to add to Administrators group

    Returns:
        Account info dict
    """
    logger.info(f"Setting up user on: {url}")

    client = GerritDevClient(url)
    admin_id = client.become_admin()
    logger.info(f"Authenticated as admin account {admin_id}")

    result: dict[str, Any] = client.setup_user_with_ssh_keys(
        username=username,
        ssh_keys=ssh_keys,
        name=name,
        email=email,
        add_to_admins=add_to_admins,
    )
    return result
