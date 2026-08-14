# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared vocabulary for the g2p GitHub validation suite.

Owns the two things every GitHub-side check module needs but which
carry no behaviour of their own:

* :class:`G2PCheckResult` — the uniform result record every check
  returns, so that reporting code can treat all checks alike.
* The GitHub API endpoints and the ``GERRIT_*`` naming contract
  (required workflow inputs, org secrets, and org variables) that
  ``gerrit_to_platform`` depends on.

This module is a leaf: it depends on nothing else in the g2p codebase,
which lets both the transport layer and every check module build on it
without risking a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"
"""Base URL for the GitHub REST API."""

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
"""URL for the GitHub GraphQL API."""

REQUIRED_WORKFLOW_INPUTS: tuple[str, ...] = (
    "GERRIT_BRANCH",
    "GERRIT_CHANGE_ID",
    "GERRIT_CHANGE_NUMBER",
    "GERRIT_CHANGE_URL",
    "GERRIT_EVENT_TYPE",
    "GERRIT_PATCHSET_NUMBER",
    "GERRIT_PATCHSET_REVISION",
    "GERRIT_PROJECT",
    "GERRIT_REFSPEC",
)
"""Standard ``GERRIT_*`` inputs every g2p workflow must accept."""

REQUIRED_ORG_SECRETS: tuple[str, ...] = ("GERRIT_SSH_PRIVKEY",)
"""Secrets that must exist at the org level."""

OPTIONAL_ORG_SECRETS: tuple[str, ...] = ("GERRIT_SSH_PRIVKEY_G2G",)
"""Secrets that are optional and reported only for visibility.

The single entry here, ``GERRIT_SSH_PRIVKEY_G2G``, is the SSH
private key used by **gerrit-to-gerrit (G2G) replication** — i.e.
when a Gerrit instance pushes changes to *another* Gerrit instance
rather than to GitHub.  Most LF deployments do not run G2G
replication; they only mirror Gerrit → GitHub via the G2P workflow
this action configures.

Because the key is irrelevant to the standard Gerrit → GitHub
flow, an absent ``GERRIT_SSH_PRIVKEY_G2G`` is reported with
``passed=True, severity='info'`` so it neither appears in the
warning stream nor blocks the run.  Orgs that *do* perform G2G
replication should populate the secret out of band; the audit
will record it as ``found`` once present.
"""

REQUIRED_ORG_VARIABLES: tuple[str, ...] = (
    "GERRIT_SERVER",
    "GERRIT_SSH_USER",
    "GERRIT_KNOWN_HOSTS",
    "GERRIT_URL",
)
"""Variables that must exist at the org level."""


# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------


@dataclass
class G2PCheckResult:
    """Outcome of a single GitHub-side validation check.

    Attributes:
        check_name: Machine-readable name (e.g. ``"token_valid"``).
        passed: Whether the check succeeded.
        message: Human-readable description of the outcome.
        severity: One of ``"error"``, ``"warning"``, or ``"info"``.
        details: Optional extra data for debugging.
    """

    check_name: str
    passed: bool
    message: str
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        status = "✅" if self.passed else "❌"
        return f"{status} [{self.severity}] {self.check_name}: {self.message}"
