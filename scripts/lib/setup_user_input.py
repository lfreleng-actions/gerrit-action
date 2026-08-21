# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Logging setup, SSH key collection and username validation.

Split out of ``setup-gerrit-user.py``: these are the steps that turn
command-line arguments and the environment into the values the setup
run needs, before any Gerrit instance is contacted.  Every name here
is re-exported from ``setup-gerrit-user.py``.
"""

from __future__ import annotations

import logging
import os

from setup_user_model import _USERNAME_MAX_LEN, _USERNAME_RE

from gerrit_api import parse_ssh_keys

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


def read_ssh_keys(
    key_string: str | None = None,
    key_file: str | None = None,
    env_var: str = "SSH_AUTH_KEYS",
) -> list[str]:
    """
    Read SSH keys from various sources.

    Args:
        key_string: SSH key(s) as a string
        key_file: Path to file containing SSH key(s)
        env_var: Environment variable name containing SSH keys

    Returns:
        List of SSH public key strings
    """
    keys = []

    # Read from file
    if key_file:
        try:
            with open(key_file) as f:
                content = f.read()
                keys.extend(parse_ssh_keys(content))
                logger.debug(f"Read {len(keys)} keys from {key_file}")
        except OSError as e:
            logger.warning(f"Failed to read key file {key_file}: {e}")

    # Read from string
    if key_string:
        keys.extend(parse_ssh_keys(key_string))

    # Read from environment
    env_keys = os.environ.get(env_var, "")
    if env_keys:
        keys.extend(parse_ssh_keys(env_keys))
        logger.debug(f"Read keys from ${env_var}")

    return keys


def collect_ssh_keys(
    key_strings: list[str] | None,
    key_files: list[str] | None,
) -> list[str]:
    """
    Gather SSH keys from the command line and the environment.

    Args:
        key_strings: Keys passed with ``--ssh-key``
        key_files: Files passed with ``--ssh-key-file``

    Returns:
        List of SSH public key strings
    """
    ssh_keys: list[str] = []
    for key in key_strings or []:
        ssh_keys.extend(parse_ssh_keys(key))
    for key_file in key_files or []:
        ssh_keys.extend(read_ssh_keys(key_file=key_file))
    # Also check environment
    ssh_keys.extend(read_ssh_keys())
    return ssh_keys


def validate_username(username: str) -> str | None:
    """Validate a username, returning an error message or None if valid."""
    if not _USERNAME_RE.match(username):
        return (
            f"Invalid username: '{username}' – "
            "must contain only letters, numbers, dots, underscores, and hyphens"
        )
    if len(username) > _USERNAME_MAX_LEN:
        return f"Username too long (max {_USERNAME_MAX_LEN} characters)"
    return None
