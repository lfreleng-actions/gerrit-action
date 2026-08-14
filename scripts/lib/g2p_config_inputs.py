# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Vocabulary and decoding for the ``g2p_*`` action inputs.

GitHub Actions delivers every input as a string, so the boundary
between the action and :class:`~g2p_config.G2PConfig` needs two
things, both of which live here:

* The accepted vocabulary — allowed enum values for the mode-style
  inputs, the recognised Gerrit hook names, and the LF defaults
  applied when an input is omitted.
* The decoders that turn a raw input string into the Python value the
  configuration model holds: booleans, comma-separated lists, the
  JSON comment-keyword mapping, and the Base64-wrapped org token map.

Every decoder raises :class:`~errors.ConfigError` with a message that
names the offending action input, so a malformed value is reported in
the operator's terms rather than as a stray parser traceback.
"""

from __future__ import annotations

import json
from typing import Any

from errors import ConfigError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_NAME_STYLES: tuple[str, ...] = ("dash", "underscore", "slash")
"""Allowed values for ``remote_name_style``."""

VALID_VALIDATION_MODES: tuple[str, ...] = ("error", "warn", "skip")
"""Allowed values for ``validation_mode``."""

VALID_ORG_SETUP_MODES: tuple[str, ...] = ("provision", "verify", "skip")
"""Allowed values for ``org_setup``."""

VALID_HOOKS: tuple[str, ...] = (
    "patchset-created",
    "comment-added",
    "change-merged",
)
"""Gerrit hook names that g2p can handle."""

DEFAULT_COMMENT_MAPPINGS: dict[str, str] = {
    "recheck": "verify",
    "reverify": "verify",
    "remerge": "merge",
}
"""Standard LF keyword-to-workflow-filter mappings."""

DEFAULT_REMOTE_AUTH_GROUP: str = "GitHub Replication"
"""Default Gerrit auth group for the GitHub replication remote."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str_to_bool(value: str) -> bool:
    """Convert a string to a boolean (case-insensitive)."""
    return value.strip().lower() in ("true", "1", "yes")


def _parse_csv(value: str) -> list[str]:
    """Split a comma-separated string into a trimmed, non-empty list."""
    return [item.strip() for item in value.split(",") if item.strip()]


def decode_org_tokens(
    b64_value: str,
) -> dict[str, str]:
    """Decode a Base64-encoded JSON array of org-token mappings.

    The expected inner JSON schema is::

        [{"github_org": "org-name", "token": "ghp_xxx"}, ...]

    Parameters
    ----------
    b64_value:
        Base64-encoded string.

    Returns
    -------
    dict[str, str]
        Mapping of ``github_org`` to ``token``.

    Raises
    ------
    ConfigError
        If the value cannot be decoded or parsed.
    """
    import base64
    import binascii

    if not b64_value.strip():
        return {}

    # Normalize input: remove all whitespace so wrapped base64
    # (e.g. line-wrapped by macOS/Linux or GitHub secrets) still
    # decodes correctly with validate=True.
    normalized = "".join(b64_value.split())

    try:
        raw = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError(
            f"Failed to decode g2p_org_token_map — bad base64: {exc}"
        ) from exc

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            "Failed to decode g2p_org_token_map — "
            f"decoded bytes are not valid UTF-8: {exc}"
        ) from exc

    try:
        entries = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Failed to parse g2p_org_token_map — bad JSON: {exc}"
        ) from exc

    if not isinstance(entries, list):
        raise ConfigError(
            f"g2p_org_token_map must be a JSON array, got {type(entries).__name__}"
        )

    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(
                f"g2p_org_token_map entries must be objects, got {type(entry).__name__}"
            )
        org = entry.get("github_org", "")
        token = entry.get("token", "")
        if not org or not token:
            raise ConfigError(
                "g2p_org_token_map entries must have 'github_org' and 'token' fields"
            )
        result[org] = token

    return result


def _parse_comment_mappings(raw: str) -> dict[str, str]:
    """Parse a JSON string into a comment-keyword mapping dict.

    Parameters
    ----------
    raw:
        JSON object string, e.g. ``'{"recheck": "verify"}'``.

    Returns
    -------
    dict[str, str]
        Parsed mapping.

    Raises
    ------
    ConfigError
        If *raw* is not valid JSON or not a flat string→string object.
    """
    if not raw.strip():
        return dict(DEFAULT_COMMENT_MAPPINGS)

    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"g2p_comment_mappings is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(
            f"g2p_comment_mappings must be a JSON object, got {type(parsed).__name__}"
        )

    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(
                "g2p_comment_mappings values must all be strings; "
                f"found {key!r}: {value!r}"
            )

    return dict(parsed)
