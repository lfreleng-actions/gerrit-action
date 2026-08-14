# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reachability checks for GitHub credentials, accounts and repos.

Answers the first question the g2p audit must settle: *can we see the
things we are about to configure?*  Every check here is a plain
existence/permission probe against the GitHub REST API — the token
itself, the target org (falling back to a user account), the
``.github`` magic repository, and any explicitly listed
``g2p_validate_repos``.

These checks form the head of the audit dependency chain: if they
fail, the org-configuration and workflow checks cannot produce
meaningful answers.
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError

from g2p_github_model import GITHUB_API_BASE, G2PCheckResult
from g2p_github_transport import github_request


def check_token_valid(token: str) -> G2PCheckResult:
    """Verify the token is valid by calling ``GET /user``.

    Parameters
    ----------
    token:
        GitHub PAT to validate.

    Returns
    -------
    G2PCheckResult
        Passed if ``GET /user`` returns 200.
    """
    try:
        status, data = github_request(f"{GITHUB_API_BASE}/user", token)
    except URLError as exc:
        return G2PCheckResult(
            check_name="token_valid",
            passed=False,
            message=f"Network error checking token: {exc}",
            severity="error",
        )

    if status == 200:
        login = data.get("login", "unknown") if isinstance(data, dict) else "unknown"
        return G2PCheckResult(
            check_name="token_valid",
            passed=True,
            message=f"Token valid (authenticated as {login})",
            severity="info",
            details={"login": login},
        )

    return G2PCheckResult(
        check_name="token_valid",
        passed=False,
        message=f"Token authentication failed (HTTP {status})",
        severity="error",
        details={"status": status},
    )


def check_org_access(token: str, owner: str) -> G2PCheckResult:
    """Verify the token can access the target organisation.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub organisation or user login.

    Returns
    -------
    G2PCheckResult
        Passed if ``GET /orgs/{owner}`` returns 200.
    """
    try:
        status, data = github_request(f"{GITHUB_API_BASE}/orgs/{owner}", token)
    except URLError as exc:
        return G2PCheckResult(
            check_name="org_access",
            passed=False,
            message=f"Network error checking org {owner}: {exc}",
            severity="error",
        )

    if status == 200:
        return G2PCheckResult(
            check_name="org_access",
            passed=True,
            message=f"Organisation '{owner}' is accessible",
            severity="info",
        )

    if status == 404:
        # Could be a user account instead of an org — try /users
        user_status = 0
        user_error = ""
        try:
            user_status, _ = github_request(f"{GITHUB_API_BASE}/users/{owner}", token)
        except URLError as user_exc:
            user_status = 0
            user_error = str(user_exc)

        if user_status == 200:
            return G2PCheckResult(
                check_name="org_access",
                passed=True,
                message=f"'{owner}' is a user account (not an org)",
                severity="info",
                details={"account_type": "user"},
            )

        # Build a message that includes the user-check outcome
        msg = f"Organisation '{owner}' not found (HTTP 404)"
        if user_error:
            msg += f"; user check also failed: {user_error}"
        elif user_status != 0:
            msg += f"; user check returned HTTP {user_status}"

        return G2PCheckResult(
            check_name="org_access",
            passed=False,
            message=msg,
            severity="error",
            details={
                "org_status": 404,
                "user_status": user_status,
            },
        )

    return G2PCheckResult(
        check_name="org_access",
        passed=False,
        message=f"Org access check failed for '{owner}' (HTTP {status})",
        severity="error",
        details={"status": status},
    )


def check_magic_repo(token: str, owner: str) -> G2PCheckResult:
    """Verify the ``.github`` magic repository exists.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub organisation or user login.

    Returns
    -------
    G2PCheckResult
        Passed if ``GET /repos/{owner}/.github`` returns 200.
    """
    try:
        status, _ = github_request(f"{GITHUB_API_BASE}/repos/{owner}/.github", token)
    except URLError as exc:
        return G2PCheckResult(
            check_name="magic_repo",
            passed=False,
            message=f"Network error checking .github repo: {exc}",
            severity="warning",
        )

    if status == 200:
        return G2PCheckResult(
            check_name="magic_repo",
            passed=True,
            message=f"Repository '{owner}/.github' exists",
            severity="info",
        )

    if status == 404:
        return G2PCheckResult(
            check_name="magic_repo",
            passed=False,
            message=(
                f"Repository '{owner}/.github' not found"
                " — required workflows will not work"
            ),
            severity="warning",
        )

    if status in (401, 403):
        return G2PCheckResult(
            check_name="magic_repo",
            passed=False,
            message=(
                f"Unable to access repository '{owner}/.github' "
                f"(HTTP {status} — authentication or permission issue). "
                "Required workflows will be inaccessible."
            ),
            severity="error",
        )

    return G2PCheckResult(
        check_name="magic_repo",
        passed=False,
        message=(
            f"Failed to check repository '{owner}/.github' "
            f"(HTTP {status}). Required workflows may not work."
        ),
        severity="warning",
    )


def check_repos_exist(
    token: str,
    owner: str,
    repos: list[str],
) -> G2PCheckResult:
    """Check that specified repositories exist via the REST API.

    Makes individual ``GET /repos/{owner}/{repo}`` calls for each
    repository in the list.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub org or user.
    repos:
        List of repository names to verify.

    Returns
    -------
    G2PCheckResult
        Passed if all repositories were found.
    """
    if not repos:
        return G2PCheckResult(
            check_name="repos_exist",
            passed=True,
            message="No repositories specified for validation",
            severity="info",
        )

    found_names: set[str] = set()
    missing: list[str] = []
    archived: list[str] = []

    for repo in repos:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
        try:
            status, data = github_request(url, token)
        except URLError as exc:
            return G2PCheckResult(
                check_name="repos_exist",
                passed=False,
                message=f"Network error checking repositories: {exc}",
                severity="warning",
            )

        if status == 404:
            missing.append(repo)
            continue

        if status != 200:
            return G2PCheckResult(
                check_name="repos_exist",
                passed=False,
                message=f"HTTP {status} checking repo '{repo}'",
                severity="warning",
                details={"status": status, "repo": repo},
            )

        if isinstance(data, dict):
            found_names.add(data.get("name", repo))
            if data.get("archived", False):
                archived.append(data.get("name", repo))
        else:
            found_names.add(repo)

    details: dict[str, Any] = {
        "found": sorted(found_names),
        "missing": missing,
        "archived": archived,
    }

    if missing:
        return G2PCheckResult(
            check_name="repos_exist",
            passed=False,
            message=f"Repositories not found: {missing}",
            severity="warning",
            details=details,
        )

    msg = f"All {len(repos)} repositories found in '{owner}'"
    if archived:
        msg += f" (archived: {archived})"

    return G2PCheckResult(
        check_name="repos_exist",
        passed=True,
        message=msg,
        severity="info",
        details=details,
    )
