# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Audit of org-level GitHub Actions secrets and variables.

``gerrit_to_platform`` reads the Gerrit endpoint and SSH credentials
from organisation-scoped Actions secrets and variables.  This module
enumerates both collections (paginating the REST API) and compares
what exists against the ``GERRIT_*`` contract declared in
:mod:`g2p_github_model`.

Both checks degrade to a warning rather than an error when the token
lacks org-read permission, because a token that cannot audit the org
may still be perfectly able to dispatch workflows.
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError

from g2p_github_model import (
    GITHUB_API_BASE,
    OPTIONAL_ORG_SECRETS,
    REQUIRED_ORG_SECRETS,
    REQUIRED_ORG_VARIABLES,
    G2PCheckResult,
)
from g2p_github_transport import github_request


def check_org_secrets(
    token: str,
    owner: str,
) -> G2PCheckResult:
    """Check the org has required Actions secrets.

    Uses REST: ``GET /orgs/{owner}/actions/secrets``
    Falls back gracefully on 403 (insufficient permissions).

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub organisation login.

    Returns
    -------
    G2PCheckResult
        Passed if all required secret names exist.
    """
    secret_names: set[str] = set()
    page = 1

    while True:
        url = f"{GITHUB_API_BASE}/orgs/{owner}/actions/secrets?per_page=100&page={page}"

        try:
            status, data = github_request(url, token)
        except URLError as exc:
            return G2PCheckResult(
                check_name="org_secrets",
                passed=False,
                message=f"Network error checking org secrets: {exc}",
                severity="warning",
            )

        if status == 403:
            return G2PCheckResult(
                check_name="org_secrets",
                passed=False,
                message=(
                    f"Cannot audit org secrets for '{owner}' — "
                    "insufficient permissions (classic PAT needs "
                    "'admin:org' scope, or fine-grained token needs "
                    "'Organization secrets: Read' permission)"
                ),
                severity="warning",
            )

        if status != 200:
            return G2PCheckResult(
                check_name="org_secrets",
                passed=False,
                message=(f"Failed to list org secrets for '{owner}' (HTTP {status})"),
                severity="warning",
                details={"status": status},
            )

        if not isinstance(data, dict):
            return G2PCheckResult(
                check_name="org_secrets",
                passed=False,
                message="Unexpected response format from org secrets API",
                severity="warning",
            )

        page_secrets = data.get("secrets", [])
        if not page_secrets:
            break

        secret_names.update(
            s["name"] for s in page_secrets if isinstance(s, dict) and "name" in s
        )

        if len(page_secrets) < 100:
            break

        page += 1

    missing_required = [s for s in REQUIRED_ORG_SECRETS if s not in secret_names]
    missing_optional = [s for s in OPTIONAL_ORG_SECRETS if s not in secret_names]
    found = [
        s for s in (*REQUIRED_ORG_SECRETS, *OPTIONAL_ORG_SECRETS) if s in secret_names
    ]

    details: dict[str, Any] = {
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "found": found,
    }

    if missing_required:
        return G2PCheckResult(
            check_name="org_secrets",
            passed=False,
            message=(
                f"Org '{owner}' is missing required secret(s): {missing_required}"
            ),
            severity="error",
            details=details,
        )

    msg = f"All required org secrets present in '{owner}'"
    if missing_optional:
        # Optional secrets (currently only GERRIT_SSH_PRIVKEY_G2G,
        # used for gerrit-to-gerrit replication) are recorded for
        # visibility but never demote the result to a warning or
        # failure: most deployments do not need them, and surfacing
        # them as advisories created noise on every run without
        # actionable signal.  See OPTIONAL_ORG_SECRETS for context.
        msg += f" (optional missing: {missing_optional})"

    return G2PCheckResult(
        check_name="org_secrets",
        passed=True,
        message=msg,
        severity="info",
        details=details,
    )


def check_org_variables(
    token: str,
    owner: str,
) -> G2PCheckResult:
    """Check the org has required Actions variables.

    Uses REST: ``GET /orgs/{owner}/actions/variables``
    Also checks that variable values are non-empty.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub organisation login.

    Returns
    -------
    G2PCheckResult
        Passed if all required variables exist and hold data.
    """
    var_map: dict[str, str] = {}
    page = 1

    while True:
        url = (
            f"{GITHUB_API_BASE}/orgs/{owner}/actions/variables?per_page=100&page={page}"
        )

        try:
            status, data = github_request(url, token)
        except URLError as exc:
            return G2PCheckResult(
                check_name="org_variables",
                passed=False,
                message=f"Network error checking org variables: {exc}",
                severity="warning",
            )

        if status == 403:
            return G2PCheckResult(
                check_name="org_variables",
                passed=False,
                message=(
                    f"Cannot audit org variables for '{owner}' — "
                    "insufficient permissions (classic PAT needs "
                    "'admin:org' scope, or fine-grained token needs "
                    "'Organization variables: Read' permission)"
                ),
                severity="warning",
            )

        if status != 200:
            return G2PCheckResult(
                check_name="org_variables",
                passed=False,
                message=(f"Failed to list org variables for '{owner}' (HTTP {status})"),
                severity="warning",
                details={"status": status},
            )

        if not isinstance(data, dict):
            return G2PCheckResult(
                check_name="org_variables",
                passed=False,
                message=("Unexpected response format from org variables API"),
                severity="warning",
            )

        page_vars = data.get("variables", [])
        if not page_vars:
            break

        for v in page_vars:
            if isinstance(v, dict) and "name" in v:
                var_map[v["name"]] = v.get("value", "")

        if len(page_vars) < 100:
            break

        page += 1

    missing = [v for v in REQUIRED_ORG_VARIABLES if v not in var_map]
    empty = [
        v for v in REQUIRED_ORG_VARIABLES if v in var_map and not var_map[v].strip()
    ]
    found = [v for v in REQUIRED_ORG_VARIABLES if v in var_map]

    details: dict[str, Any] = {
        "missing": missing,
        "empty": empty,
        "found": found,
    }

    if missing:
        return G2PCheckResult(
            check_name="org_variables",
            passed=False,
            message=(f"Org '{owner}' is missing required variable(s): {missing}"),
            severity="error",
            details=details,
        )

    if empty:
        return G2PCheckResult(
            check_name="org_variables",
            passed=False,
            message=(f"Org '{owner}' has empty variable(s): {empty}"),
            severity="warning",
            details=details,
        )

    return G2PCheckResult(
        check_name="org_variables",
        passed=True,
        message=(f"All required org variables present and populated in '{owner}'"),
        severity="info",
        details=details,
    )
