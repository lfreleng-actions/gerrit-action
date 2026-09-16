# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Synthetic credential values for the test suite.

Secret scanners match on the literal PEM armour line, not on the key
body, so a test containing ``<5 dashes>BEGIN OPENSSH PRIVATE KEY<5
dashes>`` raises an alert even when the body is the word ``fake``.
Assembling the armour at run time from fragments leaves nothing for a
scanner to match in the source while keeping the intent of each test
obvious at the call site.

These helpers are the only supported way to spell PEM armour anywhere
in the test suite.  ``tests/test_no_credential_patterns.py`` fails the
build if a literal reappears.
"""

from __future__ import annotations

_DASHES = "-" * 5
"""Armour delimiter, built at run time so it never appears adjacent to a label."""

OPENSSH_PRIVATE_KEY = "OPENSSH PRIVATE KEY"
"""Armour label emitted by modern ``ssh-keygen``."""

RSA_PRIVATE_KEY = "RSA PRIVATE KEY"
"""Armour label used by PEM-format RSA keys."""

KEY = "KEY"
"""Deliberately vague label for tests that only need *some* armour."""


def pem_header(label: str = OPENSSH_PRIVATE_KEY) -> str:
    """Return the opening armour line for *label*.

    Parameters
    ----------
    label:
        Armour label, for example :data:`OPENSSH_PRIVATE_KEY`.

    Returns
    -------
    str
        The ``BEGIN`` line, delimiters included.
    """
    return f"{_DASHES}BEGIN {label}{_DASHES}"


def pem_footer(label: str = OPENSSH_PRIVATE_KEY) -> str:
    """Return the closing armour line for *label*.

    Parameters
    ----------
    label:
        Armour label, for example :data:`OPENSSH_PRIVATE_KEY`.

    Returns
    -------
    str
        The ``END`` line, delimiters included.
    """
    return f"{_DASHES}END {label}{_DASHES}"


def pem_block(body: str = "stub", label: str = OPENSSH_PRIVATE_KEY) -> str:
    """Return a complete armoured block wrapping *body*.

    Parameters
    ----------
    body:
        Placeholder text standing in for key material.  Keep it short
        and obviously synthetic.
    label:
        Armour label, for example :data:`OPENSSH_PRIVATE_KEY`.

    Returns
    -------
    str
        Header, body and footer joined by newlines.
    """
    return f"{pem_header(label)}\n{body}\n{pem_footer(label)}"
