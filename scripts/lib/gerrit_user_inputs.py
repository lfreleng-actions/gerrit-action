# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validation and collection of Gerrit user-setup inputs.

The user-setup entry point accepts a username and SSH public keys from
three interchangeable sources: command-line flags, key files, and
environment variables.  This module turns that scattered input into
two trusted values — a validated username and a flat list of public
keys — before anything is sent to Gerrit.

The username check matters beyond tidiness: the username reaches
shell and SSH contexts elsewhere in the action, so it is restricted to
an explicit safe character set rather than merely escaped.
"""

from __future__ import annotations

import logging
import os
import re

from gerrit_api import parse_ssh_keys

logger = logging.getLogger(__name__)

# Username validation: only safe characters to prevent command injection
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_USERNAME_MAX_LEN = 64


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
    """Gather SSH public keys from every supported input source.

    Sources are read in a fixed order — inline keys, then key files,
    then ``$SSH_AUTH_KEYS`` — so the resulting list is stable across
    runs regardless of how the action was invoked.

    Args:
        key_strings: Inline keys passed via ``--ssh-key``.
        key_files: Paths passed via ``--ssh-key-file``.

    Returns:
        List of SSH public key strings, in source order.
    """
    ssh_keys: list[str] = []
    for key in key_strings or []:
        ssh_keys.extend(parse_ssh_keys(key))
    for key_file in key_files or []:
        ssh_keys.extend(read_ssh_keys(key_file=key_file))
    # Also check environment
    ssh_keys.extend(read_ssh_keys())
    return ssh_keys
