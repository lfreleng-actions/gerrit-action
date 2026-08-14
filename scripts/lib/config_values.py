# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Scalar parsing and normalisation primitives for gerrit-action config.

Owns the small, pure conversions that turn the raw strings arriving from
environment variables and JSON inputs into the typed values that the
configuration dataclasses expose:

* Time-interval strings (``"60s"``, ``"5m"``, ``"1h"``) to seconds.
* Boolean-ish strings to :class:`bool`.
* API path prefixes to a canonical leading-slash form.

These live in their own module so that both :mod:`config` and
:mod:`config_instances` can share a single definition without either
importing the other.
"""

from __future__ import annotations

import re

from errors import ConfigError

# ---------------------------------------------------------------------------
# Interval parsing
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([smhSMH]?)$")


def parse_interval_to_seconds(interval: str) -> int:
    """Parse a time interval string (e.g. ``"60s"``, ``"5m"``, ``"1h"``) to seconds.

    Plain integers (e.g. ``"60"``) are treated as seconds.

    Raises :class:`ConfigError` for invalid formats.
    """
    m = _INTERVAL_RE.match(interval.strip())
    if not m:
        raise ConfigError(
            f"Invalid interval '{interval}'. "
            "Expected format: <integer>[s|m|h], e.g. 60s, 5m, 1h"
        )
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    return value  # seconds (or no unit)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _str_to_bool(value: str) -> bool:
    """Convert a string to bool (``"true"`` → True, anything else → False)."""
    return value.strip().lower() == "true"


def _is_zero_interval(interval: str) -> bool:
    """Return True if *interval* represents zero (``"0"``, ``"0s"``, etc.)."""
    m = _INTERVAL_RE.match(interval.strip())
    if not m:
        return False
    return int(m.group(1)) == 0


def _normalise_path(path: str) -> str:
    """Normalise an API path prefix.

    * Ensures a leading ``/``.
    * Strips a trailing ``/``.
    * Collapses bare ``"/"`` to ``""``.
    * Returns ``""`` for empty input.
    """
    path = path.strip()
    if not path:
        return ""
    if not path.startswith("/"):
        path = f"/{path}"
    path = path.rstrip("/")
    if path == "/":
        return ""
    return path
